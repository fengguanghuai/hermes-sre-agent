"""ClusterProvider implementation for standalone Docker / docker compose hosts.

Mapping onto the pod-oriented pipeline:
- namespace  -> compose project (`com.docker.compose.project`), "standalone" when absent, "all" lists everything
- deployment -> compose service (`com.docker.compose.service`), or the container name
- pod / uid  -> container name / full container ID
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import DependencyCheckResult, DependencyTarget, PodSnapshot
from .kubectl import probe_dependency

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ALL_NAMESPACES = "all"
STANDALONE = "standalone"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _age_seconds(iso_ts: str | None) -> int:
    if not iso_ts or iso_ts.startswith("0001-"):
        return 0
    try:
        # Docker emits nanosecond precision; trim to microseconds for fromisoformat.
        main, _, frac = iso_ts.rstrip("Z").partition(".")
        dt = datetime.fromisoformat(f"{main}.{frac[:6] or '0'}+00:00")
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except ValueError:
        return 0


def snapshot_from_inspect(item: dict[str, Any], host_memory_mi: int | None = None) -> PodSnapshot:
    """Translate one `docker inspect` object into a PodSnapshot."""
    state = item.get("State", {}) or {}
    labels = (item.get("Config", {}) or {}).get("Labels", {}) or {}
    host_cfg = item.get("HostConfig", {}) or {}
    name = str(item.get("Name", "")).lstrip("/")

    status = str(state.get("Status", "unknown"))
    health = str(((state.get("Health") or {}).get("Status")) or "none")
    oom = bool(state.get("OOMKilled", False))
    exit_code = int(state.get("ExitCode", 0) or 0)

    reason = ""
    ready = False
    if status == "running":
        phase = "Running"
        if health == "unhealthy":
            reason = "Unhealthy"
        elif health == "starting":
            reason = ""
        else:
            ready = True
    elif status == "restarting":
        phase = "Running"
        reason = "OOMKilled" if oom else "CrashLoopBackOff"
    elif status == "exited":
        if exit_code == 0 and not oom:
            phase = "Succeeded"
        else:
            phase = "Failed"
            reason = "OOMKilled" if oom else "Error"
    elif status == "created":
        phase = "Pending"
    elif status == "paused":
        phase = "Running"
        reason = "Paused"
    else:
        phase = "Failed"
        reason = "Dead" if status == "dead" else status

    memory_bytes = int(host_cfg.get("Memory", 0) or 0)
    return PodSnapshot(
        namespace=str(labels.get(PROJECT_LABEL) or STANDALONE),
        name=name,
        uid=str(item.get("Id", "")),
        deployment=str(labels.get(SERVICE_LABEL) or name),
        container_name=name,
        phase=phase,
        ready=ready,
        reason=reason,
        exit_code=exit_code if status in ("exited", "restarting") or oom else None,
        restarts=int(item.get("RestartCount", 0) or 0),
        age_seconds=_age_seconds(state.get("StartedAt") or item.get("Created")),
        resource_version=str(state.get("StartedAt", "")),
        memory_limit_mi=(memory_bytes // (1024 * 1024)) if memory_bytes else None,
        node_allocatable_memory_mi=host_memory_mi,
        last_state_reason="OOMKilled" if oom else "",
    )


def health_events(item: dict[str, Any]) -> list[str]:
    log = ((item.get("State", {}) or {}).get("Health") or {}).get("Log") or []
    lines = []
    for entry in log[-5:]:
        if int(entry.get("ExitCode", 0) or 0) != 0:
            output = " ".join(str(entry.get("Output", "")).split())[:300]
            lines.append(f"Unhealthy: Health check failed: {output}")
    return lines


def _event_line(evt: dict[str, Any]) -> str | None:
    action = str(evt.get("Action") or evt.get("status") or "")
    attrs = (evt.get("Actor", {}) or {}).get("Attributes", {}) or {}
    if action == "oom":
        return "OOM: container exceeded its memory limit"
    if action == "die":
        return f"Died: exitCode={attrs.get('exitCode', '?')}"
    if action in ("restart", "kill", "stop", "start"):
        return f"{action.capitalize()}: container {action}"
    if action.startswith("health_status"):
        return f"HealthStatus: {action.split(':', 1)[-1].strip()}"
    return None


class DockerProvider:
    """Diagnose and heal containers on a standalone Docker host via the `docker` CLI."""

    def __init__(
        self,
        context: str | None = None,
        host: str | None = None,
        timeout_seconds: int = 30,
        reconcile_timeout_seconds: int = 30,
        reconcile_interval_seconds: float = 3.0,
        runner: Runner | None = None,
    ) -> None:
        self.context = context
        self.host = host
        self.timeout_seconds = timeout_seconds
        self.reconcile_timeout_seconds = reconcile_timeout_seconds
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self._runner = runner
        self._host_memory_mi: int | None = None
        self._host_memory_loaded = False

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker"]
        if self.host:
            cmd.extend(["-H", self.host])
        if self.context:
            cmd.extend(["--context", self.context])
        cmd.extend(args)
        if self._runner is not None:
            proc = self._runner(cmd)
        else:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if args[:1] == ["logs"] else subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker failed ({proc.returncode}): {(proc.stderr or proc.stdout or '').strip()}")
        return proc

    def _host_memory(self) -> int | None:
        if not self._host_memory_loaded:
            self._host_memory_loaded = True
            proc = self._run(["info", "--format", "{{.MemTotal}}"], check=False)
            try:
                self._host_memory_mi = int(proc.stdout.strip()) // (1024 * 1024)
            except ValueError:
                self._host_memory_mi = None
        return self._host_memory_mi

    def _inspect(self, refs: list[str]) -> list[dict[str, Any]]:
        if not refs:
            return []
        return json.loads(self._run(["inspect", *refs]).stdout or "[]")

    def list_pods(self, namespace: str) -> list[PodSnapshot]:
        args = ["ps", "-a", "-q", "--no-trunc"]
        if namespace not in (ALL_NAMESPACES, STANDALONE):
            args.extend(["--filter", f"label={PROJECT_LABEL}={namespace}"])
        ids = self._run(args).stdout.split()
        host_mem = self._host_memory()
        pods = [snapshot_from_inspect(item, host_mem) for item in self._inspect(ids)]
        if namespace == STANDALONE:
            pods = [p for p in pods if p.namespace == STANDALONE]
        return pods

    def get_pod(self, namespace: str, pod_name: str) -> PodSnapshot:
        items = self._inspect([pod_name])
        if not items:
            raise RuntimeError(f"Container {pod_name!r} not found")
        return snapshot_from_inspect(items[0], self._host_memory())

    def get_pod_events(self, namespace: str, pod_name: str) -> list[str]:
        items = self._inspect([pod_name])
        lines = health_events(items[0]) if items else []
        proc = self._run(
            [
                "events",
                "--since", "1h",
                "--until", str(int(time.time())),
                "--filter", f"container={pod_name}",
                "--format", "{{json .}}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            for raw in proc.stdout.splitlines()[-25:]:
                try:
                    line = _event_line(json.loads(raw))
                except json.JSONDecodeError:
                    continue
                if line:
                    lines.append(line)
        return lines

    def get_pod_logs(self, namespace: str, pod_name: str, tail_lines: int = 80) -> list[str]:
        proc = self._run(["logs", f"--tail={tail_lines}", pod_name], check=False)
        if proc.returncode != 0:
            return []
        return (proc.stdout or "").splitlines()[-tail_lines:]

    def check_dependencies(self, targets: list[DependencyTarget]) -> list[DependencyCheckResult]:
        return [probe_dependency(t) for t in targets]

    def _require_same_container(self, pod_name: str, expected_id: str) -> dict[str, Any]:
        items = self._inspect([pod_name])
        current = str(items[0].get("Id", "")) if items else ""
        if current != expected_id:
            raise RuntimeError(
                f"Container ID mismatch for {pod_name}: expected {expected_id[:12]}, got {current[:12] or 'none'}"
            )
        return items[0]

    def patch_deployment_memory(
        self,
        namespace: str,
        deployment: str,
        container_name: str,
        expected_pod_uid: str,
        expected_resource_version: str,
        new_memory_limit_mi: int,
        new_memory_request_mi: int,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        item = self._require_same_container(container_name, expected_pod_uid)
        labels = (item.get("Config", {}) or {}).get("Labels", {}) or {}
        update = ["update", "--memory", f"{new_memory_limit_mi}m", "--memory-swap", f"{new_memory_limit_mi}m", expected_pod_uid]
        status = str((item.get("State", {}) or {}).get("Status", ""))
        details: dict[str, Any] = {
            "container": container_name,
            "container_id": expected_pod_uid[:12],
            "new_memory_limit_mi": new_memory_limit_mi,
            "commands": ["docker " + " ".join(update)] + ([f"docker start {expected_pod_uid[:12]}"] if status == "exited" else []),
        }
        if labels.get(PROJECT_LABEL):
            details["persist_hint"] = (
                f"Set mem_limit: {new_memory_limit_mi}m for service '{deployment}' in the compose file, "
                "otherwise the next `docker compose up` reverts it."
            )
        if dry_run:
            details["status"] = "DRY_RUN_VALIDATED"
            return details
        self._run(update)
        if status == "exited":
            self._run(["start", expected_pod_uid])
        details["status"] = "APPLIED_REQUIRES_APP_VALIDATION"
        return details

    def delete_pod_by_uid(
        self,
        namespace: str,
        pod_name: str,
        expected_pod_uid: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self._require_same_container(pod_name, expected_pod_uid)
        details: dict[str, Any] = {
            "container": pod_name,
            "container_id": expected_pod_uid[:12],
            "commands": [f"docker restart -t 10 {expected_pod_uid[:12]}"],
        }
        if dry_run:
            details["status"] = "DRY_RUN_VALIDATED"
            return details
        # Restart by full ID so a same-named replacement container is never touched.
        self._run(["restart", "-t", "10", expected_pod_uid])
        details["status"] = "APPLIED_REQUIRES_APP_VALIDATION"
        return details

    def verify_reconciliation(
        self,
        namespace: str,
        deployment: str,
        old_pod_uid: str,
        action: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.reconcile_timeout_seconds
        stable_checks = 0
        last: PodSnapshot | None = None
        while True:
            items = self._inspect([old_pod_uid])
            last = snapshot_from_inspect(items[0]) if items else None
            if last is not None and last.ready:
                stable_checks += 1
                if stable_checks >= 2:
                    break
            else:
                stable_checks = 0
            if time.monotonic() >= deadline:
                break
            time.sleep(self.reconcile_interval_seconds)
        verified = stable_checks >= 2
        return {
            "verified": verified,
            "action": action,
            "container": last.name if last else None,
            "observed_state": f"{last.phase}/{last.reason or ('ready' if last.ready else 'not-ready')}" if last else "missing",
            "status": "RESOLVED" if verified else "MITIGATED_PENDING_OBSERVATION",
        }
