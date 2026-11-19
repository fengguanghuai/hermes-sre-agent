"""Core data models for the 5-stage diagnosis and bounded self-healing pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DependencyTarget:
    name: str
    kind: str  # "tcp" | "http"
    endpoint: str  # "host:port" or "http(s)://..."
    timeout_seconds: float = 2.0


@dataclass(frozen=True)
class DependencyCheckResult:
    name: str
    kind: str
    endpoint: str
    healthy: bool
    latency_ms: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class PodSnapshot:
    namespace: str
    name: str
    uid: str
    deployment: str
    container_name: str
    phase: str  # Running | Pending | Failed | Succeeded | Unknown
    ready: bool
    reason: str = ""  # OOMKilled | CrashLoopBackOff | ImagePullBackOff | CreateContainerConfigError | ...
    exit_code: int | None = None
    restarts: int = 0
    age_seconds: int = 0
    terminating_seconds: int | None = None
    resource_version: str = ""
    memory_limit_mi: int | None = None
    memory_request_mi: int | None = None
    node_allocatable_memory_mi: int | None = None
    namespace_quota_remaining_mi: int | None = None
    last_state_reason: str = ""


@dataclass
class InventorySummary:
    namespace: str
    environment: str
    abnormal: list[PodSnapshot] = field(default_factory=list)
    transitioning: list[PodSnapshot] = field(default_factory=list)
    succeeded: list[PodSnapshot] = field(default_factory=list)
    healthy_count: int = 0
    note: str = (
        "Container-state snapshot only; does not claim application logs, "
        "business endpoints, or downstream middleware are healthy."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "environment": self.environment,
            "healthy_count": self.healthy_count,
            "abnormal_count": len(self.abnormal),
            "transitioning_count": len(self.transitioning),
            "succeeded_count": len(self.succeeded),
            "abnormal": [asdict(p) for p in self.abnormal],
            "transitioning": [asdict(p) for p in self.transitioning],
            "succeeded": [asdict(p) for p in self.succeeded],
            "note": self.note,
        }


@dataclass
class Observation:
    namespace: str
    environment: str
    pod: PodSnapshot
    events: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    dependencies: list[DependencyCheckResult] = field(default_factory=list)
    observed_at: str = ""
    redacted_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "environment": self.environment,
            "pod": asdict(self.pod),
            "events": self.events,
            "logs": self.logs,
            "dependencies": [asdict(d) for d in self.dependencies],
            "observed_at": self.observed_at,
            "redacted_count": self.redacted_count,
        }


@dataclass(frozen=True)
class EvidenceHit:
    source: str
    pattern: str
    weight: float
    kind: str  # "positive" | "negative"
    reason: str = ""


@dataclass
class Classification:
    root_cause: str
    confidence: float
    base_score: float
    evidence: list[EvidenceHit] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "confidence": round(self.confidence, 4),
            "base_score": round(self.base_score, 4),
            "evidence": [asdict(e) for e in self.evidence],
            "rejected_candidates": self.rejected_candidates,
            "summary": self.summary,
        }


@dataclass
class PolicyDecision:
    action: str  # resize_memory | recreate_pod | dispatch_issue
    allowed: bool
    require_approval: bool
    risk: str  # low | medium | high | critical
    blocked_by: str | None = None
    reason: str = ""
    cooldown_layer: str | None = None
    target_memory_mi: int | None = None
    sizing_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionReceipt:
    request_id: str
    idempotency_key: str
    namespace: str
    environment: str
    deployment: str
    pod_name: str
    pod_uid: str
    root_cause: str
    action: str
    mode: str  # dry_run | applied | blocked | escalated
    lifecycle_state: str  # open | investigating | mitigated | resolved | escalated
    executed_at: str
    details: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentReport:
    request_id: str
    lifecycle_state: str
    observation: Observation
    classification: Classification
    decision: PolicyDecision
    receipt: ActionReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "lifecycle_state": self.lifecycle_state,
            "observation": self.observation.to_dict(),
            "classification": self.classification.to_dict(),
            "decision": self.decision.to_dict(),
            "receipt": self.receipt.to_dict() if self.receipt else None,
        }
