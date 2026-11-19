"""5-stage workflow orchestrator, Markdown renderers, and scenario benchmark runner."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .classifier import Classifier
from .control_plane import ControlPlane
from .executor import Executor
from .models import DependencyTarget, IncidentReport, InventorySummary
from .observer import Observer
from .policy import PolicyBundle
from .providers.base import ClusterProvider
from .providers.replay import ScenarioReplayProvider
from .reconciler import ReceiptStore, Reconciler


class SREWorkflow:
    """Orchestrates Observation -> Classification -> Policy Gate -> Action -> Reconciliation."""

    def __init__(
        self,
        provider: ClusterProvider,
        policies: PolicyBundle,
        state_dir: Path,
    ) -> None:
        self.provider = provider
        self.policies = policies
        self.state_dir = state_dir
        self.observer = Observer(provider)
        self.classifier = Classifier(policies)
        self.control_plane = ControlPlane(policies, state_dir)
        self.executor = Executor(provider)
        self.reconciler = Reconciler(provider, state_dir)

    def inventory(self, namespace: str, environment: str) -> InventorySummary:
        return self.observer.scan_inventory(namespace, environment)

    def diagnose(
        self,
        *,
        namespace: str,
        environment: str,
        pod_name: str,
        request_id: str | None = None,
        dependencies: list[DependencyTarget] | None = None,
        approved: bool = False,
    ) -> IncidentReport:
        req_id = request_id or f"diag-{uuid.uuid4().hex[:10]}"
        obs = self.observer.observe_pod(
            namespace=namespace,
            environment=environment,
            pod_name=pod_name,
            dependencies=dependencies,
        )
        classification = self.classifier.classify(obs)
        decision = self.control_plane.evaluate(obs, classification, approved=approved)
        lifecycle = "investigating" if decision.allowed else "escalated"
        return IncidentReport(
            request_id=req_id,
            lifecycle_state=lifecycle,
            observation=obs,
            classification=classification,
            decision=decision,
            receipt=None,
        )

    def heal(
        self,
        *,
        namespace: str,
        environment: str,
        pod_name: str,
        request_id: str | None = None,
        dependencies: list[DependencyTarget] | None = None,
        apply: bool = False,
        approved: bool = False,
    ) -> IncidentReport:
        req_id = request_id or f"heal-{uuid.uuid4().hex[:10]}"
        existing = self.reconciler.store.load(req_id)
        if existing is not None:
            raise RuntimeError(
                f"Receipt already exists for request_id={req_id!r} "
                f"(mode={existing.get('mode')}, state={existing.get('lifecycle_state')}). "
                f"Use `hermes-sre status --request-id {req_id}` instead of replaying."
            )

        obs = self.observer.observe_pod(
            namespace=namespace,
            environment=environment,
            pod_name=pod_name,
            dependencies=dependencies,
        )
        classification = self.classifier.classify(obs)
        decision = self.control_plane.evaluate(obs, classification, approved=approved)
        receipt = self.executor.execute(
            request_id=req_id,
            obs=obs,
            classification=classification,
            decision=decision,
            apply=apply,
        )
        if receipt.mode == "applied":
            self.control_plane.cooldown_store.record(
                namespace=obs.namespace,
                environment=obs.environment,
                deployment=obs.pod.deployment,
                pod_name=obs.pod.name,
                pod_uid=obs.pod.uid,
                root_cause=classification.root_cause,
                action=decision.action,
            )
        receipt = self.reconciler.reconcile(receipt)
        return IncidentReport(
            request_id=req_id,
            lifecycle_state=receipt.lifecycle_state,
            observation=obs,
            classification=classification,
            decision=decision,
            receipt=receipt,
        )

    def status(self, request_id: str) -> dict[str, Any] | None:
        return ReceiptStore(self.state_dir).load(request_id)


def run_benchmark(
    scenarios_dir: Path,
    policies: PolicyBundle,
) -> dict[str, Any]:
    """Run all deterministic scenario fixtures and validate expected control-plane outcomes."""
    scenario_files = sorted(scenarios_dir.glob("*.json"))
    results: list[dict[str, Any]] = []
    passed = 0
    total_ms = 0.0

    for path in scenario_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="hermes-sre-bench-") as tmp:
            state_dir = Path(tmp)
            # Pre-seed cooldown history if scenario tests anti-avalanche breaker
            seed_history = data.get("seed_history", [])
            if seed_history:
                now_iso = datetime.now(timezone.utc).isoformat()
                hydrated = []
                for item in seed_history:
                    entry = dict(item)
                    entry.setdefault("timestamp", now_iso)
                    hydrated.append(entry)
                (state_dir / "heal_history.json").write_text(
                    json.dumps(hydrated, indent=2), encoding="utf-8"
                )

            provider = ScenarioReplayProvider(data)
            workflow = SREWorkflow(provider, policies, state_dir)
            report = workflow.heal(
                namespace=data["namespace"],
                environment=data["environment"],
                pod_name=data["target_pod"],
                request_id=f"bench-{data['id']}",
                apply=bool(data.get("apply", True)),
                approved=bool(data.get("approved", False)),
            )

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
        total_ms += elapsed_ms

        expected = data.get("expected", {})
        checks = {
            "root_cause": report.classification.root_cause == expected.get("root_cause"),
            "action": report.decision.action == expected.get("action"),
            "allowed": report.decision.allowed == expected.get("allowed"),
            "lifecycle_state": report.lifecycle_state == expected.get("lifecycle_state"),
        }
        if "blocked_by" in expected:
            checks["blocked_by"] = report.decision.blocked_by == expected["blocked_by"]

        ok = all(checks.values())
        if ok:
            passed += 1
        results.append(
            {
                "id": data["id"],
                "title": data.get("title", path.stem),
                "environment": data["environment"],
                "passed": ok,
                "root_cause": report.classification.root_cause,
                "confidence": round(report.classification.confidence, 2),
                "action": report.decision.action,
                "allowed": report.decision.allowed,
                "blocked_by": report.decision.blocked_by,
                "lifecycle_state": report.lifecycle_state,
                "elapsed_ms": elapsed_ms,
                "checks": checks,
            }
        )

    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": round((passed / len(results)) * 100.0, 1) if results else 0.0,
        "total_elapsed_ms": round(total_ms, 2),
        "scenarios": results,
    }


def render_inventory_markdown(summary: InventorySummary) -> str:
    lines = [
        f"## Namespace Inventory: `{summary.namespace}` (`{summary.environment}`)",
        "",
        f"- **Healthy**: {summary.healthy_count}",
        f"- **Abnormal**: {len(summary.abnormal)}",
        f"- **Transitioning**: {len(summary.transitioning)}",
        f"- **Succeeded**: {len(summary.succeeded)}",
        "",
    ]
    if summary.abnormal:
        lines.append("### Abnormal Pods")
        lines.append("| Pod | Deployment | Phase | Reason | Restarts |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for p in summary.abnormal:
            lines.append(
                f"| `{p.name}` | `{p.deployment}` | {p.phase} | {p.reason or '-'} | {p.restarts} |"
            )
        lines.append("")
    lines.append(f"> {summary.note}")
    return "\n".join(lines)


def render_incident_markdown(report: IncidentReport) -> str:
    obs = report.observation
    cls = report.classification
    dec = report.decision
    lines = [
        f"## Incident Report: `{obs.namespace}/{obs.pod.name}` (`{obs.environment}`)",
        "",
        f"- **Request ID**: `{report.request_id}`",
        f"- **Lifecycle State**: `{report.lifecycle_state}`",
        f"- **Root Cause**: `{cls.root_cause}` (confidence `{cls.confidence:.2f}`)",
        f"- **Policy Decision**: action=`{dec.action}` | allowed=`{dec.allowed}` | risk=`{dec.risk}`",
    ]
    if dec.blocked_by:
        lines.append(f"- **Blocked By**: `{dec.blocked_by}` — {dec.reason}")
    elif dec.sizing_reason:
        lines.append(f"- **Sizing**: {dec.sizing_reason}")
    else:
        lines.append(f"- **Reason**: {dec.reason}")

    if cls.evidence:
        lines.append("")
        lines.append("### Evidence")
        for ev in cls.evidence:
            sign = "+" if ev.weight >= 0 else ""
            lines.append(
                f"- [{ev.kind}] `{ev.source}` matched `{ev.pattern}` ({sign}{ev.weight:.2f})"
                + (f" — {ev.reason}" if ev.reason else "")
            )

    if cls.rejected_candidates:
        lines.append("")
        lines.append("### Rejected Candidates")
        for rej in cls.rejected_candidates:
            lines.append(
                f"- `{rej['candidate']}` (score `{rej['confidence']}`): {'; '.join(rej['reasons'])}"
            )

    if report.receipt:
        lines.append("")
        lines.append("### Action Receipt")
        lines.append(
            f"- **Idempotency Key**: `{report.receipt.idempotency_key}` | **Mode**: `{report.receipt.mode}`"
        )
        if report.receipt.reconciliation:
            lines.append(
                f"- **Reconciliation**: `{report.receipt.reconciliation.get('status')}` "
                f"(verified=`{report.receipt.reconciliation.get('verified')}`)"
            )
    return "\n".join(lines)


def render_benchmark_markdown(result: dict[str, Any]) -> str:
    lines = [
        "## hermes-sre-agent Scenario Benchmark",
        "",
        f"**Passed**: `{result['passed']}/{result['total']}` ({result['pass_rate']}%) | "
        f"**Total Time**: `{result['total_elapsed_ms']}ms`",
        "",
        "| Scenario | Env | Root Cause | Conf | Action | Guardrail / Blocked By | Lifecycle | Pass |",
        "| --- | --- | --- | ---: | --- | --- | --- | :---: |",
    ]
    for s in result["scenarios"]:
        lines.append(
            f"| `{s['id']}` ({s['title']}) | `{s['environment']}` | `{s['root_cause']}` | "
            f"{s['confidence']:.2f} | `{s['action']}` | `{s['blocked_by'] or 'allowed'}` | "
            f"`{s['lifecycle_state']}` | {'PASS' if s['passed'] else 'FAIL'} |"
        )
    return "\n".join(lines)
