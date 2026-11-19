"""Stage 3: Policy Gate, Bounded Sizing, Pre-Flight Checks, and 4-Layer Cooldown."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Classification, Observation, PolicyDecision
from .policy import PolicyBundle

_RISK_ORDER = ["low", "medium", "high", "critical"]


def _upgrade_risk(risk: str) -> str:
    try:
        idx = _RISK_ORDER.index(risk)
        return _RISK_ORDER[min(idx + 1, len(_RISK_ORDER) - 1)]
    except ValueError:
        return "high"


def _round_up_64mi(value_mi: int) -> int:
    return int(math.ceil(value_mi / 64.0) * 64)


class CooldownStore:
    """Persistent JSON history backing the 4-layer anti-avalanche circuit breaker."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.history_path = state_dir / "heal_history.json"

    def load(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, records: list[dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def cleanup(self, retain_days: int, now: datetime) -> list[dict[str, Any]]:
        cutoff = now - timedelta(days=retain_days)
        kept = [r for r in self.load() if self._parse_ts(r.get("timestamp")) >= cutoff]
        self.save(kept)
        return kept

    def record(
        self,
        *,
        namespace: str,
        environment: str,
        deployment: str,
        pod_name: str,
        pod_uid: str,
        root_cause: str,
        action: str,
        now: datetime | None = None,
    ) -> None:
        ts = (now or datetime.now(timezone.utc)).isoformat()
        records = self.load()
        records.append(
            {
                "timestamp": ts,
                "namespace": namespace,
                "environment": environment,
                "deployment": deployment,
                "pod_name": pod_name,
                "pod_uid": pod_uid,
                "root_cause": root_cause,
                "action": action,
            }
        )
        self.save(records)

    @staticmethod
    def _parse_ts(raw: str | None) -> datetime:
        if not raw:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)


class ControlPlane:
    """Evaluates heal policy, environment guardrails, preflight checks, and cooldowns."""

    def __init__(self, policies: PolicyBundle, state_dir: Path) -> None:
        self.policies = policies
        self.cooldown_store = CooldownStore(state_dir)

    def evaluate(
        self,
        obs: Observation,
        classification: Classification,
        *,
        approved: bool = False,
        now: datetime | None = None,
    ) -> PolicyDecision:
        current_time = now or datetime.now(timezone.utc)
        heal_cfg = self.policies.heal
        conf_cfg = self.policies.confidence
        cool_cfg = self.policies.cooldown

        auto_heal_min = float(conf_cfg.get("thresholds", {}).get("auto_heal_min", 0.75))
        policy_map = heal_cfg.get("policies", {})
        root_policy = policy_map.get(
            classification.root_cause, policy_map.get("UNKNOWN", {})
        )
        action = str(root_policy.get("action", "dispatch_issue"))
        risk = str(root_policy.get("risk", "high"))
        require_approval = bool(root_policy.get("require_approval", True))

        env_map = heal_cfg.get("env_constraints", {})
        env_key = obs.environment.lower()
        env_rule = env_map.get(env_key, env_map.get("prod", {}))
        if bool(env_rule.get("risk_upgrade", False)):
            risk = _upgrade_risk(risk)
        if bool(env_rule.get("require_approval", False)):
            require_approval = True
        cooldown_mult = int(env_rule.get("cooldown_multiplier", 1))

        # Gate 1: Confidence threshold
        if classification.confidence < auto_heal_min:
            return PolicyDecision(
                action="dispatch_issue",
                allowed=False,
                require_approval=True,
                risk=risk,
                blocked_by="confidence_threshold",
                reason=(
                    f"Confidence {classification.confidence:.2f} is below "
                    f"auto_heal_min ({auto_heal_min:.2f}). {classification.summary}"
                ),
            )

        # Gate 2: Policy itself escalates (e.g., CODE_DEFECT / CONFIG_MISSING)
        if action == "dispatch_issue":
            return PolicyDecision(
                action="dispatch_issue",
                allowed=False,
                require_approval=True,
                risk=risk,
                blocked_by="infra_heal_boundary",
                reason=str(root_policy.get("description", "Outside infra self-heal boundary.")),
            )

        # Gate 3: Pre-flight startup log guard before pod deletion
        if action == "recreate_pod":
            combined_logs = "\n".join(obs.logs + obs.events)
            for pattern in heal_cfg.get("preflight_block_patterns", []) or []:
                if re.search(str(pattern), combined_logs, flags=re.IGNORECASE):
                    return PolicyDecision(
                        action="dispatch_issue",
                        allowed=False,
                        require_approval=True,
                        risk="high",
                        blocked_by="preflight_log_guard",
                        reason=(
                            f"Blocked blind pod recreation: matched deterministic error "
                            f"pattern {pattern!r} in startup logs/events."
                        ),
                    )

        # Gate 4: Environment / policy approval requirement
        if require_approval and not approved:
            return PolicyDecision(
                action=action,
                allowed=False,
                require_approval=True,
                risk=risk,
                blocked_by="approval_required",
                reason=(
                    f"Action {action!r} in environment {obs.environment!r} requires "
                    f"explicit approval (pass --approve after review)."
                ),
            )

        # Gate 5: 4-Layer Anti-Avalanche Cooldown Circuit Breaker
        history_cfg = cool_cfg.get("history", {})
        if bool(history_cfg.get("cleanup_on_run", True)):
            records = self.cooldown_store.cleanup(
                int(history_cfg.get("retain_days", 30)), current_time
            )
        else:
            records = self.cooldown_store.load()

        violation = self._check_cooldowns(
            obs=obs,
            root_cause=classification.root_cause,
            records=records,
            cool_cfg=cool_cfg.get("cooldowns", {}),
            multiplier=cooldown_mult,
            now=current_time,
        )
        if violation is not None:
            layer, reason = violation
            return PolicyDecision(
                action="dispatch_issue",
                allowed=False,
                require_approval=True,
                risk="critical" if layer == "cluster_pressure" else risk,
                blocked_by=f"cooldown:{layer}",
                cooldown_layer=layer,
                reason=reason,
            )

        # Gate 6: Bounded memory sizing & capacity checks for OOM resize
        target_mem_mi: int | None = None
        sizing_reason = ""
        if action == "resize_memory":
            target_mem_mi, sizing_reason, block_reason = self._compute_bounded_memory(
                obs=obs, root_policy=root_policy, records=records
            )
            if block_reason is not None:
                return PolicyDecision(
                    action="dispatch_issue",
                    allowed=False,
                    require_approval=True,
                    risk="high",
                    blocked_by="memory_bounds",
                    reason=block_reason,
                )

        return PolicyDecision(
            action=action,
            allowed=True,
            require_approval=require_approval,
            risk=risk,
            reason=str(root_policy.get("description", "Allowed by policy.")),
            target_memory_mi=target_mem_mi,
            sizing_reason=sizing_reason,
        )

    def _check_cooldowns(
        self,
        *,
        obs: Observation,
        root_cause: str,
        records: list[dict[str, Any]],
        cool_cfg: dict[str, Any],
        multiplier: int,
        now: datetime,
    ) -> tuple[str, str] | None:
        # Layer 4: Cluster-wide panic breaker (checked first — highest priority)
        cluster_rule = cool_cfg.get("cluster_pressure", {})
        win_min = int(cluster_rule.get("window_minutes", 1))
        max_cluster = int(cluster_rule.get("max_actions", 5))
        cluster_cutoff = now - timedelta(minutes=win_min)
        recent_cluster = [
            r
            for r in records
            if r.get("environment") == obs.environment
            and self.cooldown_store._parse_ts(r.get("timestamp")) >= cluster_cutoff
        ]
        if len(recent_cluster) >= max_cluster:
            return (
                "cluster_pressure",
                f"PANIC CIRCUIT BREAKER: {len(recent_cluster)} actions in "
                f"{win_min}m (limit {max_cluster}) in env={obs.environment}. "
                f"Automation halted; escalate immediately.",
            )

        # Layer 1: Same Pod + Same Root Cause
        l1 = cool_cfg.get("same_pod_same_root", {})
        l1_hours = int(l1.get("hours", 24)) * multiplier
        l1_cutoff = now - timedelta(hours=l1_hours)
        for r in records:
            if (
                r.get("namespace") == obs.namespace
                and r.get("pod_name") == obs.pod.name
                and r.get("root_cause") == root_cause
                and self.cooldown_store._parse_ts(r.get("timestamp")) >= l1_cutoff
            ):
                return (
                    "same_pod_same_root",
                    f"Cooldown L1 hit: pod {obs.pod.name} already healed for "
                    f"{root_cause} within {l1_hours}h.",
                )

        # Layer 2: Same Pod + Any Root Cause
        l2 = cool_cfg.get("same_pod_any_root", {})
        l2_hours = int(l2.get("hours", 12)) * multiplier
        l2_cutoff = now - timedelta(hours=l2_hours)
        for r in records:
            if (
                r.get("namespace") == obs.namespace
                and r.get("pod_name") == obs.pod.name
                and self.cooldown_store._parse_ts(r.get("timestamp")) >= l2_cutoff
            ):
                return (
                    "same_pod_any_root",
                    f"Cooldown L2 hit: pod {obs.pod.name} had another root cause "
                    f"({r.get('root_cause')}) healed within {l2_hours}h; multi-root fault escalated.",
                )

        # Layer 3: Same Deployment rate-limit
        l3 = cool_cfg.get("same_deployment", {})
        l3_hours = int(l3.get("hours", 6)) * multiplier
        l3_max = int(l3.get("max_actions", 1))
        l3_cutoff = now - timedelta(hours=l3_hours)
        recent_deploy = [
            r
            for r in records
            if r.get("namespace") == obs.namespace
            and r.get("deployment") == obs.pod.deployment
            and self.cooldown_store._parse_ts(r.get("timestamp")) >= l3_cutoff
        ]
        if len(recent_deploy) >= l3_max:
            return (
                "same_deployment",
                f"Cooldown L3 hit: deployment {obs.pod.deployment} reached "
                f"{len(recent_deploy)}/{l3_max} actions within {l3_hours}h; rate-limited to prevent outage.",
            )

        return None

    @staticmethod
    def _compute_bounded_memory(
        *,
        obs: Observation,
        root_policy: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> tuple[int | None, str, str | None]:
        current_limit = obs.pod.memory_limit_mi
        if current_limit is None:
            return (
                None,
                "",
                "Container has no memory limit configured; refusing to invent a limit blindly.",
            )

        floor_mi = int(root_policy.get("memory_floor_mi", 512))
        ceiling_mi = int(root_policy.get("memory_ceiling_mi", 16384))
        scale_factor = float(root_policy.get("scale_factor", 1.5))
        max_auto = int(root_policy.get("max_auto_resizes", 1))

        prior_resizes = [
            r
            for r in records
            if r.get("namespace") == obs.namespace
            and r.get("deployment") == obs.pod.deployment
            and r.get("action") == "resize_memory"
        ]
        if len(prior_resizes) >= max_auto:
            return (
                None,
                "",
                f"Deployment {obs.pod.deployment} already had {len(prior_resizes)} "
                f"auto memory resize(s) (max {max_auto}); recurrent OOM requires leak investigation.",
            )

        if current_limit >= ceiling_mi:
            return (
                None,
                "",
                f"Current memory limit {current_limit}Mi is already at/above ceiling {ceiling_mi}Mi.",
            )

        proposed = max(floor_mi, _round_up_64mi(int(current_limit * scale_factor)))
        proposed = min(proposed, ceiling_mi)
        delta = proposed - current_limit

        if (
            obs.pod.node_allocatable_memory_mi is not None
            and proposed > obs.pod.node_allocatable_memory_mi
        ):
            return (
                None,
                "",
                f"Proposed memory {proposed}Mi exceeds node allocatable "
                f"{obs.pod.node_allocatable_memory_mi}Mi.",
            )
        if (
            obs.pod.namespace_quota_remaining_mi is not None
            and delta > obs.pod.namespace_quota_remaining_mi
        ):
            return (
                None,
                "",
                f"Additional memory +{delta}Mi exceeds namespace quota remaining "
                f"{obs.pod.namespace_quota_remaining_mi}Mi.",
            )

        reason = (
            f"OOMKilled at {current_limit}Mi -> scaled x{scale_factor} within "
            f"[{floor_mi}Mi, {ceiling_mi}Mi] to {proposed}Mi."
        )
        return proposed, reason, None
