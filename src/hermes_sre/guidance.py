"""Turn a diagnosis/heal outcome into user-facing next steps and follow-up questions."""

from __future__ import annotations

import re
from typing import Any

from .models import ActionReceipt, Classification, Observation, PolicyDecision

_CONFIG_KEY_PATTERNS = [
    r"Could not resolve placeholder '([^']+)'",
    r"Missing required environment variable[:\s]+['\"]?([A-Za-z0-9_.\-]+)",
]
_EXCEPTION_PATTERN = r"((?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*(?:Error|Exception))[:\s]*(.*)"


def _first_match(patterns: list[str], lines: list[str]) -> str | None:
    for line in lines:
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                return m.group(1)
    return None


def _ask(question: str, how_to_supply: str = "") -> dict[str, str]:
    item = {"question": question}
    if how_to_supply:
        item["how_to_supply"] = how_to_supply
    return item


def build_next_steps(
    obs: Observation,
    cls: Classification,
    dec: PolicyDecision,
    receipt: ActionReceipt | None = None,
) -> dict[str, Any]:
    """Explain what happened, what the user can do, and what extra info the agent needs."""
    target = f"{obs.namespace}/{obs.pod.name}"
    lines = obs.logs + obs.events
    user_actions: list[str] = []
    ask_user: list[dict[str, str]] = []

    if receipt is not None and receipt.mode == "applied":
        if receipt.lifecycle_state == "resolved":
            return {
                "summary": f"{dec.action} applied on {target} and the replacement is Ready.",
                "needs_user_input": False,
                "user_actions": ["Keep watching business metrics/logs for a while to confirm full recovery."],
                "ask_user": [],
            }
        return {
            "summary": f"{dec.action} applied on {target}, but recovery is not verified yet.",
            "needs_user_input": False,
            "user_actions": [
                f"Wait 1-3 minutes, then check `hermes-sre status --request-id {receipt.request_id}` "
                "or re-run diagnose on the new pod."
            ],
            "ask_user": [],
        }

    if dec.allowed:
        return {
            "summary": f"Safe to run {dec.action} on {target}; this was a dry-run, nothing changed.",
            "needs_user_input": True,
            "user_actions": [],
            "ask_user": [
                _ask(
                    f"Confirm to execute {dec.action} on {target}?"
                    + (f" ({dec.sizing_reason})" if dec.sizing_reason else ""),
                    "re-run heal with --apply and a NEW --request-id",
                )
            ],
        }

    blocked = dec.blocked_by or ""

    if blocked == "approval_required":
        ask_user.append(
            _ask(
                f"{obs.environment} requires human approval. Approve {dec.action} on {target} (risk={dec.risk})?",
                "re-run heal with --apply --approve and a NEW --request-id",
            )
        )
        summary = f"Fix is available but needs your approval in {obs.environment}."

    elif blocked == "confidence_threshold":
        summary = f"Root cause of {target} is unclear (confidence {cls.confidence:.2f}); not auto-fixing."
        for rej in cls.rejected_candidates:
            reasons = "; ".join(r for r in rej.get("reasons", []) if r)
            if reasons:
                user_actions.append(f"Ruled out {rej['candidate']}: {reasons}")
        if any(r.get("candidate") == "OOM_KILLED" for r in cls.rejected_candidates):
            user_actions.append(
                "Compare app startup time with startupProbe (failureThreshold x periodSeconds); "
                "raise the probe budget if startup is legitimately slow."
            )
            ask_user.append(_ask("How long does this service normally take to start? Was the probe or JVM config changed recently?"))
        if not obs.dependencies:
            ask_user.append(
                _ask(
                    "Which downstream dependencies (DB/Redis/MQ/HTTP) does this service use? I can probe them.",
                    "re-run diagnose with --dep tcp:<name>:<host:port> or --dep http:<name>:<url>",
                )
            )
        ask_user.append(_ask("Was there a release, config change, or node maintenance right before the failure?"))

    elif blocked in ("infra_heal_boundary", "preflight_log_guard"):
        root = cls.root_cause
        config_key = _first_match(_CONFIG_KEY_PATTERNS, lines)
        if root == "CONFIG_MISSING" or config_key:
            key = config_key or "the missing setting"
            summary = f"{target} fails because configuration `{key}` is missing; restarting will not help."
            user_actions.append(f"Add `{key}` in the config center / ConfigMap / Secret for {obs.environment}.")
            ask_user.append(_ask(f"Tell me once `{key}` is added and I will re-diagnose and recreate the pod."))
        elif root == "CODE_DEFECT":
            exc = _first_match([_EXCEPTION_PATTERN], lines) or "an application exception"
            summary = f"{target} crashes on `{exc}`; this is a code/package defect outside infra self-healing."
            user_actions.append("Send the evidence to the owning developers, or roll back to the last good version.")
            ask_user.append(_ask("Was a new version deployed recently? Share the version/commit so the issue can be routed."))
        elif root == "DEPENDENCY_UNREACHABLE":
            failed = [d for d in obs.dependencies if not d.healthy]
            names = ", ".join(f"{d.name}({d.endpoint})" for d in failed) or "a downstream dependency"
            summary = f"{target} cannot reach {names}; restarting the caller will not fix it."
            user_actions.append(f"Check the health/network/whitelist of {names}.")
            ask_user.append(_ask("Tell me when the dependency is back, or give me another endpoint to probe.", "--dep ..."))
        else:
            summary = f"Auto-fix for {target} is blocked: {dec.reason}"
            user_actions.append("Fix the deterministic error shown in the evidence, then re-diagnose.")
            ask_user.append(_ask("Tell me once the error is fixed and I will re-diagnose."))

    elif blocked.startswith("cooldown:"):
        summary = f"Auto-fix paused by anti-avalanche protection ({dec.cooldown_layer}): {dec.reason}"
        if dec.cooldown_layer == "cluster_pressure":
            user_actions.append("Many services are being healed at once; check for a cluster-wide outage (node, network, registry, storage).")
        else:
            user_actions.append("Repeated failures after an earlier fix usually mean a deeper issue; investigate manually.")
        ask_user.append(_ask("Should I hand this over to the on-call engineer with the collected evidence?"))

    elif blocked == "memory_bounds":
        summary = f"Memory resize for {target} is not safe: {dec.reason}"
        user_actions.append("Investigate a memory leak or set the memory limit manually after capacity review.")
        ask_user.append(_ask("Do you want a heap/memory investigation checklist, or will you adjust the limit manually?"))

    else:
        summary = f"Auto-fix for {target} is blocked: {dec.reason}"
        ask_user.append(_ask("How would you like to proceed?"))

    return {
        "summary": summary,
        "needs_user_input": bool(ask_user),
        "user_actions": user_actions,
        "ask_user": ask_user,
    }


def render_next_steps_markdown(next_steps: dict[str, Any]) -> list[str]:
    if not next_steps:
        return []
    lines = ["", "### Next Steps", f"- {next_steps['summary']}"]
    for action in next_steps.get("user_actions", []):
        lines.append(f"- {action}")
    if next_steps.get("ask_user"):
        lines.append("")
        lines.append("### Need From You")
        for item in next_steps["ask_user"]:
            hint = f" (how: {item['how_to_supply']})" if item.get("how_to_supply") else ""
            lines.append(f"- {item['question']}{hint}")
    return lines
