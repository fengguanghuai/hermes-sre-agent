"""Deterministic ScenarioReplayProvider for offline testing, CI, and demos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import DependencyCheckResult, DependencyTarget, PodSnapshot


class ScenarioReplayProvider:
    """In-memory Kubernetes provider replaying structured JSON incident fixtures."""

    def __init__(self, scenario_data: dict[str, Any]) -> None:
        self.data = scenario_data
        self._pods: dict[str, PodSnapshot] = {}
        for raw in scenario_data.get("pods", []):
            snap = PodSnapshot(**raw)
            self._pods[snap.name] = snap
        self._events: dict[str, list[str]] = dict(scenario_data.get("events", {}))
        self._logs: dict[str, list[str]] = dict(scenario_data.get("logs", {}))
        self._dep_results: list[DependencyCheckResult] = [
            DependencyCheckResult(**d) for d in scenario_data.get("dependencies", [])
        ]
        self._reconcile_outcome: dict[str, Any] = dict(
            scenario_data.get(
                "reconciliation",
                {
                    "verified": True,
                    "status": "RESOLVED",
                    "replacement_pods": [],
                    "note": "Replacement pod became Ready with zero recurrent OOM/crash events.",
                },
            )
        )
        self.applied_mutations: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path) -> "ScenarioReplayProvider":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def list_pods(self, namespace: str) -> list[PodSnapshot]:
        return [p for p in self._pods.values() if p.namespace == namespace]

    def get_pod(self, namespace: str, pod_name: str) -> PodSnapshot:
        if pod_name not in self._pods:
            raise KeyError(f"Pod {namespace}/{pod_name} not found in scenario")
        return self._pods[pod_name]

    def get_pod_events(self, namespace: str, pod_name: str) -> list[str]:
        return list(self._events.get(pod_name, []))

    def get_pod_logs(self, namespace: str, pod_name: str, tail_lines: int = 80) -> list[str]:
        return list(self._logs.get(pod_name, []))[-tail_lines:]

    def check_dependencies(
        self, targets: list[DependencyTarget]
    ) -> list[DependencyCheckResult]:
        if self._dep_results:
            return list(self._dep_results)
        return []

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
        matched = [p for p in self._pods.values() if p.uid == expected_pod_uid]
        if not matched:
            raise RuntimeError(f"CAS mismatch: pod UID {expected_pod_uid} no longer exists")
        pod = matched[0]
        if expected_resource_version and pod.resource_version != expected_resource_version:
            raise RuntimeError(
                f"CAS mismatch: resourceVersion {pod.resource_version} != {expected_resource_version}"
            )
        record = {
            "status": "DRY_RUN_VALIDATED" if dry_run else "APPLIED_REQUIRES_APP_VALIDATION",
            "action": "resize_memory",
            "namespace": namespace,
            "deployment": deployment,
            "container": container_name,
            "expected_pod_uid": expected_pod_uid,
            "expected_resource_version": expected_resource_version,
            "previous_memory_limit_mi": pod.memory_limit_mi,
            "new_memory_limit_mi": new_memory_limit_mi,
            "new_memory_request_mi": new_memory_request_mi,
        }
        if not dry_run:
            self.applied_mutations.append(record)
        return record

    def delete_pod_by_uid(
        self,
        namespace: str,
        pod_name: str,
        expected_pod_uid: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        pod = self.get_pod(namespace, pod_name)
        if pod.uid != expected_pod_uid:
            raise RuntimeError(
                f"Pod UID mismatch: expected {expected_pod_uid}, got {pod.uid}"
            )
        record = {
            "status": "DRY_RUN_VALIDATED" if dry_run else "APPLIED_REQUIRES_APP_VALIDATION",
            "action": "recreate_pod",
            "namespace": namespace,
            "pod": pod_name,
            "pod_uid": expected_pod_uid,
        }
        if not dry_run:
            self.applied_mutations.append(record)
        return record

    def verify_reconciliation(
        self,
        namespace: str,
        deployment: str,
        old_pod_uid: str,
        action: str,
    ) -> dict[str, Any]:
        outcome = dict(self._reconcile_outcome)
        if not outcome.get("replacement_pods"):
            outcome["replacement_pods"] = [f"{deployment}-reconciled-0"]
        outcome["action"] = action
        return outcome
