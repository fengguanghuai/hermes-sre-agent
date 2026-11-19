"""CLI entrypoint for hermes-sre-agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .models import DependencyTarget
from .policy import PolicyBundle
from .providers.base import ClusterProvider
from .providers.kubectl import KubectlProvider
from .providers.replay import ScenarioReplayProvider
from .workflow import (
    SREWorkflow,
    render_benchmark_markdown,
    render_incident_markdown,
    render_inventory_markdown,
    run_benchmark,
)


def _default_state_dir() -> Path:
    return Path.cwd() / ".hermes-sre-state"


def _default_scenarios_dir() -> Path:
    repo_candidate = Path(__file__).resolve().parents[2] / "scenarios"
    if repo_candidate.is_dir():
        return repo_candidate
    return Path.cwd() / "scenarios"


def _parse_dependencies(raw_deps: list[str] | None) -> list[DependencyTarget]:
    targets: list[DependencyTarget] = []
    for raw in raw_deps or []:
        parts = raw.split(":", 2)
        if len(parts) != 3:
            raise ValueError(
                f"Invalid --dep format {raw!r}; expected '<tcp|http>:<name>:<endpoint>'"
            )
        kind, name, endpoint = parts
        targets.append(DependencyTarget(name=name, kind=kind.lower(), endpoint=endpoint))
    return targets


def _build_provider(args: argparse.Namespace) -> ClusterProvider:
    if getattr(args, "scenario", None):
        return ScenarioReplayProvider.from_file(Path(args.scenario))
    return KubectlProvider(
        kubeconfig=getattr(args, "kubeconfig", None),
        context=getattr(args, "context", None),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-sre",
        description="Policy-driven Kubernetes diagnosis and bounded self-healing CLI.",
    )
    parser.add_argument(
        "--policy-dir",
        type=Path,
        default=None,
        help="Directory containing confidence/heal/cooldown YAML policies.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_default_state_dir(),
        help="Directory for cooldown history and idempotent action receipts.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_cluster_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--namespace", "-n", required=True, help="Kubernetes namespace")
        p.add_argument(
            "--env",
            default="staging",
            choices=["dev", "staging", "prod"],
            help="Target environment tier (default: staging)",
        )
        p.add_argument("--scenario", type=Path, default=None, help="Offline scenario JSON file")
        p.add_argument("--kubeconfig", default=None, help="Optional kubeconfig path")
        p.add_argument("--context", default=None, help="Optional kubectl context")
        p.add_argument(
            "--format",
            default="json",
            choices=["json", "markdown"],
            help="Output format (default: json)",
        )

    p_inv = sub.add_parser("inventory", help="Read-only namespace abnormal pod snapshot")
    add_common_cluster_args(p_inv)

    p_diag = sub.add_parser("diagnose", help="Read-only single-pod diagnosis and policy plan")
    add_common_cluster_args(p_diag)
    p_diag.add_argument("--pod", required=True, help="Target pod name")
    p_diag.add_argument("--request-id", default=None, help="Optional unique request ID")
    p_diag.add_argument(
        "--dep",
        action="append",
        default=[],
        help="Downstream dependency probe: tcp:<name>:<host:port> or http:<name>:<url>",
    )

    p_heal = sub.add_parser(
        "heal",
        help="Run 5-stage diagnosis, policy gate, bounded action (dry-run by default), and reconciliation",
    )
    add_common_cluster_args(p_heal)
    p_heal.add_argument("--pod", required=True, help="Target pod name")
    p_heal.add_argument("--request-id", default=None, help="Unique request ID for idempotency")
    p_heal.add_argument(
        "--dep",
        action="append",
        default=[],
        help="Downstream dependency probe: tcp:<name>:<host:port> or http:<name>:<url>",
    )
    p_heal.add_argument(
        "--apply",
        action="store_true",
        help="Execute approved mutation (omit for safe dry-run plan)",
    )
    p_heal.add_argument(
        "--approve",
        action="store_true",
        help="Explicit human approval for policies/environments requiring approval",
    )

    p_status = sub.add_parser("status", help="Inspect an existing action receipt by request ID")
    p_status.add_argument("--request-id", required=True, help="Request ID to inspect")

    p_bench = sub.add_parser("benchmark", help="Run all deterministic fault-injection scenarios")
    p_bench.add_argument(
        "--scenarios-dir",
        type=Path,
        default=_default_scenarios_dir(),
        help="Directory containing scenario JSON fixtures",
    )
    p_bench.add_argument(
        "--format",
        default="markdown",
        choices=["json", "markdown"],
        help="Output format (default: markdown)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    policies = PolicyBundle.load(args.policy_dir)

    if args.command == "status":
        from .reconciler import ReceiptStore

        receipt = ReceiptStore(args.state_dir).load(args.request_id)
        if receipt is None:
            print(
                json.dumps(
                    {"found": False, "request_id": args.request_id},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0

    if args.command == "benchmark":
        result = run_benchmark(args.scenarios_dir, policies)
        if args.format == "markdown":
            print(render_benchmark_markdown(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] == result["total"] else 1

    provider = _build_provider(args)
    workflow = SREWorkflow(provider, policies, args.state_dir)

    if args.command == "inventory":
        summary = workflow.inventory(args.namespace, args.env)
        if args.format == "markdown":
            print(render_inventory_markdown(summary))
        else:
            print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "diagnose":
        deps = _parse_dependencies(args.dep)
        report = workflow.diagnose(
            namespace=args.namespace,
            environment=args.env,
            pod_name=args.pod,
            request_id=args.request_id,
            dependencies=deps,
        )
        if args.format == "markdown":
            print(render_incident_markdown(report))
        else:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "heal":
        deps = _parse_dependencies(args.dep)
        try:
            report = workflow.heal(
                namespace=args.namespace,
                environment=args.env,
                pod_name=args.pod,
                request_id=args.request_id,
                dependencies=deps,
                apply=args.apply,
                approved=args.approve,
            )
        except RuntimeError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        if args.format == "markdown":
            print(render_incident_markdown(report))
        else:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
