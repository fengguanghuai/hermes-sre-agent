"""Stage 1: Read-Only Observation, Inventory Hygiene, and Secret Redaction."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import (
    DependencyTarget,
    InventorySummary,
    Observation,
    PodSnapshot,
)
from .providers.base import ClusterProvider

_SECRET_PATTERNS = [
    re.compile(
        r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|bearer|access[_-]?key)"
        r"(\s*[:=]\s*)([^\s,;\"']+)"
    ),
    re.compile(r"(?i)(postgres|mysql|redis|mongodb)://([^:\s]+):([^@\s]+)@"),
]

_EXPLICIT_ABNORMAL_REASONS = {
    "CrashLoopBackOff",
    "OOMKilled",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
    "RunContainerError",
    "Evicted",
}


def redact_lines(lines: list[str]) -> tuple[list[str], int]:
    """Redact credentials, tokens, and connection-string passwords from logs/events."""
    redacted: list[str] = []
    count = 0
    for line in lines:
        updated = line
        for idx, pattern in enumerate(_SECRET_PATTERNS):
            if idx == 0:
                updated, n = pattern.subn(r"\1\2[REDACTED]", updated)
            else:
                updated, n = pattern.subn(r"\1://\2:[REDACTED]@", updated)
            count += n
        redacted.append(updated)
    return redacted, count


def classify_inventory_pods(
    namespace: str, environment: str, pods: list[PodSnapshot]
) -> InventorySummary:
    """Apply container-state inventory hygiene rules.

    - Ready Running pods are healthy even if they have historical restarts/lastState.
    - Succeeded pods (completed Jobs) are reported separately.
    - Pods starting for < 5 minutes without explicit error are transitioning.
    - Pods terminating for <= 10 minutes are transitioning; > 10 minutes are abnormal.
    """
    summary = InventorySummary(namespace=namespace, environment=environment)
    for pod in pods:
        if pod.phase == "Succeeded":
            summary.succeeded.append(pod)
            continue
        if pod.terminating_seconds is not None:
            if pod.terminating_seconds <= 600:
                summary.transitioning.append(pod)
            else:
                summary.abnormal.append(pod)
            continue
        if pod.phase == "Running" and pod.ready:
            summary.healthy_count += 1
            continue
        if (
            pod.phase in ("Pending", "Running")
            and not pod.ready
            and pod.age_seconds < 300
            and pod.reason not in _EXPLICIT_ABNORMAL_REASONS
        ):
            summary.transitioning.append(pod)
            continue
        summary.abnormal.append(pod)
    return summary


class Observer:
    """Read-only observer collecting sanitized pod, event, log, and dependency evidence."""

    def __init__(self, provider: ClusterProvider) -> None:
        self.provider = provider

    def scan_inventory(self, namespace: str, environment: str) -> InventorySummary:
        pods = self.provider.list_pods(namespace)
        return classify_inventory_pods(namespace, environment, pods)

    def observe_pod(
        self,
        namespace: str,
        environment: str,
        pod_name: str,
        dependencies: list[DependencyTarget] | None = None,
        tail_lines: int = 80,
    ) -> Observation:
        pod = self.provider.get_pod(namespace, pod_name)
        raw_events = self.provider.get_pod_events(namespace, pod_name)
        raw_logs = self.provider.get_pod_logs(namespace, pod_name, tail_lines=tail_lines)
        events, ev_redacted = redact_lines(raw_events)
        logs, log_redacted = redact_lines(raw_logs)
        dep_results = self.provider.check_dependencies(dependencies or [])
        return Observation(
            namespace=namespace,
            environment=environment,
            pod=pod,
            events=events,
            logs=logs,
            dependencies=dep_results,
            observed_at=datetime.now(timezone.utc).isoformat(),
            redacted_count=ev_redacted + log_redacted,
        )
