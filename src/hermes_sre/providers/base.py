"""Protocol interface separating SRE logic from Kubernetes transport."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import DependencyCheckResult, DependencyTarget, PodSnapshot


class ClusterProvider(Protocol):
    def list_pods(self, namespace: str) -> list[PodSnapshot]:
        """Return container-state snapshots for all pods in namespace."""
        ...

    def get_pod(self, namespace: str, pod_name: str) -> PodSnapshot:
        """Return current snapshot for a single pod."""
        ...

    def get_pod_events(self, namespace: str, pod_name: str) -> list[str]:
        """Return recent Kubernetes events associated with the pod."""
        ...

    def get_pod_logs(self, namespace: str, pod_name: str, tail_lines: int = 80) -> list[str]:
        """Return bounded recent log lines for the pod (previous container if crashed)."""
        ...

    def check_dependencies(
        self, targets: list[DependencyTarget]
    ) -> list[DependencyCheckResult]:
        """Run read-only TCP/HTTP health checks against declared downstream endpoints."""
        ...

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
        """Atomically patch Deployment container memory limits/requests (CAS guarded)."""
        ...

    def delete_pod_by_uid(
        self,
        namespace: str,
        pod_name: str,
        expected_pod_uid: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Delete a specific pod instance only if its current UID matches."""
        ...

    def verify_reconciliation(
        self,
        namespace: str,
        deployment: str,
        old_pod_uid: str,
        action: str,
    ) -> dict[str, Any]:
        """Verify replacement pod/ReplicaSet readiness and absence of recurrent faults."""
        ...
