"""Stage 4: Bounded Action Execution with Exact Pod UID & CAS Safety."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .models import ActionReceipt, Classification, Observation, PolicyDecision
from .providers.base import ClusterProvider


def derive_idempotency_key(request_id: str, pod_uid: str, action: str) -> str:
    raw = f"{request_id}:{pod_uid}:{action}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class Executor:
    """Executes policy-approved bounded Kubernetes mutations (dry-run by default)."""

    def __init__(self, provider: ClusterProvider) -> None:
        self.provider = provider

    def execute(
        self,
        *,
        request_id: str,
        obs: Observation,
        classification: Classification,
        decision: PolicyDecision,
        apply: bool = False,
    ) -> ActionReceipt:
        now_iso = datetime.now(timezone.utc).isoformat()
        idem_key = derive_idempotency_key(request_id, obs.pod.uid, decision.action)

        if not decision.allowed:
            mode = "escalated" if decision.action == "dispatch_issue" else "blocked"
            return ActionReceipt(
                request_id=request_id,
                idempotency_key=idem_key,
                namespace=obs.namespace,
                environment=obs.environment,
                deployment=obs.pod.deployment,
                pod_name=obs.pod.name,
                pod_uid=obs.pod.uid,
                root_cause=classification.root_cause,
                action=decision.action,
                mode=mode,
                lifecycle_state="escalated",
                executed_at=now_iso,
                details={
                    "blocked_by": decision.blocked_by,
                    "reason": decision.reason,
                    "require_approval": decision.require_approval,
                },
            )

        dry_run = not apply
        details: dict[str, Any]
        if decision.action == "resize_memory":
            target_mi = decision.target_memory_mi
            if target_mi is None:
                raise ValueError("resize_memory allowed without target_memory_mi")
            req_mi = max(256, int(target_mi * 0.75))
            details = self.provider.patch_deployment_memory(
                namespace=obs.namespace,
                deployment=obs.pod.deployment,
                container_name=obs.pod.container_name,
                expected_pod_uid=obs.pod.uid,
                expected_resource_version=obs.pod.resource_version,
                new_memory_limit_mi=target_mi,
                new_memory_request_mi=req_mi,
                dry_run=dry_run,
            )
            details["sizing_reason"] = decision.sizing_reason
        elif decision.action == "recreate_pod":
            details = self.provider.delete_pod_by_uid(
                namespace=obs.namespace,
                pod_name=obs.pod.name,
                expected_pod_uid=obs.pod.uid,
                dry_run=dry_run,
            )
        else:
            raise ValueError(f"Unsupported bounded action: {decision.action}")

        return ActionReceipt(
            request_id=request_id,
            idempotency_key=idem_key,
            namespace=obs.namespace,
            environment=obs.environment,
            deployment=obs.pod.deployment,
            pod_name=obs.pod.name,
            pod_uid=obs.pod.uid,
            root_cause=classification.root_cause,
            action=decision.action,
            mode="applied" if apply else "dry_run",
            lifecycle_state="mitigated" if apply else "investigating",
            executed_at=now_iso,
            details=details,
        )
