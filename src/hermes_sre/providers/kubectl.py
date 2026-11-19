"""Live Kubernetes ClusterProvider backed by `kubectl`."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..models import DependencyCheckResult, DependencyTarget, PodSnapshot


_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$")


def parse_k8s_memory_mi(value: str | None) -> int | None:
    """Convert Kubernetes memory quantity strings (e.g. '512Mi', '2Gi') to MiB."""
    if not value:
        return None
    m = _MEM_RE.match(value.strip())
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2) or ""
    factors = {
        "": 1.0 / (1024 * 1024),
        "Ki": 1.0 / 1024,
        "K": 1.0 / 1024,
        "Mi": 1.0,
        "M": 1.0,
        "Gi": 1024.0,
        "G": 1024.0,
        "Ti": 1024.0 * 1024.0,
        "T": 1024.0 * 1024.0,
    }
    return max(1, int(round(amount * factors[unit])))


def _age_seconds(iso_ts: str | None) -> int:
    if not iso_ts:
        return 0
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except ValueError:
        return 0


def _infer_deployment(pod_item: dict[str, Any]) -> str:
    meta = pod_item.get("metadata", {})
    labels = meta.get("labels", {}) or {}
    for key in ("app.kubernetes.io/name", "app", "k8s-app"):
        if labels.get(key):
            return str(labels[key])
    owners = meta.get("ownerReferences", []) or []
    for owner in owners:
        name = str(owner.get("name", ""))
        if owner.get("kind") == "ReplicaSet" and "-" in name:
            return name.rsplit("-", 1)[0]
        if name:
            return name
    return str(meta.get("name", "unknown"))


def _pod_from_k8s_json(item: dict[str, Any]) -> PodSnapshot:
    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    containers = spec.get("containers", []) or []
    c_statuses = status.get("containerStatuses", []) or []
    primary_spec = containers[0] if containers else {}
    primary_status = c_statuses[0] if c_statuses else {}

    state = primary_status.get("state", {}) or {}
    last_state = primary_status.get("lastState", {}) or {}
    reason = ""
    exit_code: int | None = None
    if "waiting" in state:
        reason = str(state["waiting"].get("reason", ""))
    elif "terminated" in state:
        reason = str(state["terminated"].get("reason", ""))
        exit_code = state["terminated"].get("exitCode")

    last_reason = ""
    if "terminated" in last_state:
        last_reason = str(last_state["terminated"].get("reason", ""))
        if exit_code is None:
            exit_code = last_state["terminated"].get("exitCode")

    if not reason and status.get("reason"):
        reason = str(status["reason"])

    resources = primary_spec.get("resources", {}) or {}
    limits = resources.get("limits", {}) or {}
    requests = resources.get("requests", {}) or {}

    del_ts = meta.get("deletionTimestamp")
    terminating_sec = _age_seconds(del_ts) if del_ts else None

    return PodSnapshot(
        namespace=str(meta.get("namespace", "default")),
        name=str(meta.get("name", "")),
        uid=str(meta.get("uid", "")),
        deployment=_infer_deployment(item),
        container_name=str(primary_spec.get("name", "app")),
        phase=str(status.get("phase", "Unknown")),
        ready=bool(primary_status.get("ready", False)),
        reason=reason,
        exit_code=exit_code,
        restarts=int(primary_status.get("restartCount", 0)),
        age_seconds=_age_seconds(meta.get("creationTimestamp")),
        terminating_seconds=terminating_sec,
        resource_version=str(meta.get("resourceVersion", "")),
        memory_limit_mi=parse_k8s_memory_mi(limits.get("memory")),
        memory_request_mi=parse_k8s_memory_mi(requests.get("memory")),
        last_state_reason=last_reason,
    )


def probe_dependency(target: DependencyTarget) -> DependencyCheckResult:
    """Read-only TCP or HTTP dependency health probe."""
    start = time.monotonic()
    if target.kind == "tcp":
        try:
            host, port_s = target.endpoint.rsplit(":", 1)
            with socket.create_connection((host, int(port_s)), timeout=target.timeout_seconds):
                elapsed = (time.monotonic() - start) * 1000.0
                return DependencyCheckResult(
                    name=target.name,
                    kind=target.kind,
                    endpoint=target.endpoint,
                    healthy=True,
                    latency_ms=round(elapsed, 2),
                    detail="TCP handshake succeeded",
                )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000.0
            return DependencyCheckResult(
                name=target.name,
                kind=target.kind,
                endpoint=target.endpoint,
                healthy=False,
                latency_ms=round(elapsed, 2),
                detail=f"dependency_failed: {exc}",
            )

    if target.kind == "http":
        try:
            req = urllib.request.Request(target.endpoint, method="GET")
            with urllib.request.urlopen(req, timeout=target.timeout_seconds) as resp:
                elapsed = (time.monotonic() - start) * 1000.0
                ok = 200 <= resp.status < 400
                return DependencyCheckResult(
                    name=target.name,
                    kind=target.kind,
                    endpoint=target.endpoint,
                    healthy=ok,
                    latency_ms=round(elapsed, 2),
                    detail=f"HTTP {resp.status}" if ok else f"dependency_failed: HTTP {resp.status}",
                )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000.0
            return DependencyCheckResult(
                name=target.name,
                kind=target.kind,
                endpoint=target.endpoint,
                healthy=False,
                latency_ms=round(elapsed, 2),
                detail=f"dependency_failed: {exc}",
            )

    return DependencyCheckResult(
        name=target.name,
        kind=target.kind,
        endpoint=target.endpoint,
        healthy=False,
        detail=f"dependency_failed: unsupported probe kind {target.kind!r}",
    )


class KubectlProvider:
    """ClusterProvider implementation invoking `kubectl` with bounded timeouts."""

    def __init__(
        self,
        kubeconfig: str | None = None,
        context: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.timeout_seconds = timeout_seconds

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd.extend(["--kubeconfig", self.kubeconfig])
        if self.context:
            cmd.extend(["--context", self.context])
        cmd.extend(args)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"kubectl failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc

    def list_pods(self, namespace: str) -> list[PodSnapshot]:
        proc = self._run(["get", "pods", "-n", namespace, "-o", "json"])
        payload = json.loads(proc.stdout)
        return [_pod_from_k8s_json(item) for item in payload.get("items", [])]

    def get_pod(self, namespace: str, pod_name: str) -> PodSnapshot:
        proc = self._run(["get", "pod", pod_name, "-n", namespace, "-o", "json"])
        return _pod_from_k8s_json(json.loads(proc.stdout))

    def get_pod_events(self, namespace: str, pod_name: str) -> list[str]:
        proc = self._run(
            [
                "get",
                "events",
                "-n",
                namespace,
                "--field-selector",
                f"involvedObject.name={pod_name}",
                "-o",
                "json",
            ],
            check=False,
        )
        if proc.returncode != 0:
            return []
        payload = json.loads(proc.stdout)
        lines: list[str] = []
        for item in payload.get("items", [])[-25:]:
            reason = item.get("reason", "")
            msg = item.get("message", "")
            lines.append(f"{reason}: {msg}".strip(": "))
        return lines

    def get_pod_logs(self, namespace: str, pod_name: str, tail_lines: int = 80) -> list[str]:
        proc = self._run(
            ["logs", pod_name, "-n", namespace, f"--tail={tail_lines}", "--previous"],
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            proc = self._run(
                ["logs", pod_name, "-n", namespace, f"--tail={tail_lines}"],
                check=False,
            )
        if proc.returncode != 0:
            return []
        return proc.stdout.splitlines()[-tail_lines:]

    def check_dependencies(
        self, targets: list[DependencyTarget]
    ) -> list[DependencyCheckResult]:
        return [probe_dependency(t) for t in targets]

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
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": {
                                    "limits": {"memory": f"{new_memory_limit_mi}Mi"},
                                    "requests": {"memory": f"{new_memory_request_mi}Mi"},
                                },
                            }
                        ]
                    }
                }
            }
        }
        args = [
            "patch",
            "deployment",
            deployment,
            "-n",
            namespace,
            "--type=strategic",
            "-p",
            json.dumps(patch_body),
        ]
        if dry_run:
            args.append("--dry-run=server")
        proc = self._run(args)
        return {
            "status": "DRY_RUN_VALIDATED" if dry_run else "APPLIED_REQUIRES_APP_VALIDATION",
            "deployment": deployment,
            "container": container_name,
            "expected_pod_uid": expected_pod_uid,
            "expected_resource_version": expected_resource_version,
            "new_memory_limit_mi": new_memory_limit_mi,
            "new_memory_request_mi": new_memory_request_mi,
            "kubectl_output": proc.stdout.strip(),
        }

    def delete_pod_by_uid(
        self,
        namespace: str,
        pod_name: str,
        expected_pod_uid: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        current = self.get_pod(namespace, pod_name)
        if current.uid != expected_pod_uid:
            raise RuntimeError(
                f"Pod UID mismatch for {pod_name}: expected {expected_pod_uid}, got {current.uid}"
            )
        args = ["delete", "pod", pod_name, "-n", namespace]
        if dry_run:
            args.append("--dry-run=server")
        proc = self._run(args)
        return {
            "status": "DRY_RUN_VALIDATED" if dry_run else "APPLIED_REQUIRES_APP_VALIDATION",
            "pod": pod_name,
            "pod_uid": expected_pod_uid,
            "kubectl_output": proc.stdout.strip(),
        }

    def verify_reconciliation(
        self,
        namespace: str,
        deployment: str,
        old_pod_uid: str,
        action: str,
    ) -> dict[str, Any]:
        pods = [p for p in self.list_pods(namespace) if p.deployment == deployment]
        replacements = [p for p in pods if p.uid != old_pod_uid and p.ready and p.phase == "Running"]
        return {
            "verified": bool(replacements),
            "action": action,
            "replacement_pods": [p.name for p in replacements],
            "status": "RESOLVED" if replacements else "MITIGATED_PENDING_OBSERVATION",
        }
