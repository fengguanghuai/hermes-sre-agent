"""Stage 2: Deterministic Root-Cause Classification with Positive/Negative Evidence."""

from __future__ import annotations

import re
from typing import Any

from .models import Classification, EvidenceHit, Observation
from .policy import PolicyBundle


def _source_text(obs: Observation, source: str) -> str:
    if source == "status":
        parts = [
            obs.pod.phase,
            obs.pod.reason,
            obs.pod.last_state_reason,
            f"exitCode={obs.pod.exit_code}" if obs.pod.exit_code is not None else "",
        ]
        return "\n".join(p for p in parts if p)
    if source == "events":
        return "\n".join(obs.events)
    if source == "logs":
        return "\n".join(obs.logs)
    if source == "dependencies":
        return "\n".join(
            f"{d.name} healthy={d.healthy} {d.detail}" for d in obs.dependencies
        )
    return ""


class Classifier:
    """Scores candidate root causes using positive and negative evidence weights."""

    def __init__(self, policies: PolicyBundle) -> None:
        self.config = policies.confidence

    def classify(self, obs: Observation) -> Classification:
        categories: dict[str, dict[str, Any]] = self.config.get("categories", {})
        escalate_below = float(
            self.config.get("thresholds", {}).get("escalate_below", 0.55)
        )

        scored: list[tuple[float, str, float, list[EvidenceHit], str]] = []
        rejected: list[dict[str, Any]] = []

        for cat_name, spec in categories.items():
            if cat_name == "UNKNOWN":
                continue
            pos_hits: list[EvidenceHit] = []
            neg_hits: list[EvidenceHit] = []

            for sig in spec.get("positive_signals", []) or []:
                haystack = _source_text(obs, str(sig.get("source", "")))
                pattern = str(sig.get("pattern", ""))
                if pattern and re.search(pattern, haystack, flags=re.IGNORECASE):
                    pos_hits.append(
                        EvidenceHit(
                            source=str(sig.get("source", "")),
                            pattern=pattern,
                            weight=float(sig.get("weight", 0.0)),
                            kind="positive",
                        )
                    )

            if not pos_hits:
                continue

            for sig in spec.get("negative_signals", []) or []:
                haystack = _source_text(obs, str(sig.get("source", "")))
                pattern = str(sig.get("pattern", ""))
                if pattern and re.search(pattern, haystack, flags=re.IGNORECASE):
                    neg_hits.append(
                        EvidenceHit(
                            source=str(sig.get("source", "")),
                            pattern=pattern,
                            weight=float(sig.get("weight", 0.0)),
                            kind="negative",
                            reason=str(sig.get("reason", "")),
                        )
                    )

            base_score = float(spec.get("base_score", 0.5))
            raw_score = base_score + sum(h.weight for h in pos_hits) + sum(
                h.weight for h in neg_hits
            )
            clamped = max(0.0, min(1.0, raw_score))
            all_hits = pos_hits + neg_hits

            if clamped < escalate_below:
                rejected.append(
                    {
                        "candidate": cat_name,
                        "confidence": round(clamped, 4),
                        "reasons": [h.reason or h.pattern for h in neg_hits],
                    }
                )
            else:
                scored.append(
                    (
                        clamped,
                        cat_name,
                        base_score,
                        all_hits,
                        str(spec.get("description", "")),
                    )
                )

        if not scored:
            unknown_spec = categories.get("UNKNOWN", {})
            base = float(unknown_spec.get("base_score", 0.4))
            summary = str(unknown_spec.get("description", "Unclassified failure."))
            if rejected:
                top_rej = rejected[0]
                summary = (
                    f"Candidate {top_rej['candidate']} rejected by negative evidence "
                    f"(confidence={top_rej['confidence']}): "
                    f"{'; '.join(top_rej['reasons']) or 'below threshold'}."
                )
            return Classification(
                root_cause="UNKNOWN",
                confidence=base if not rejected else rejected[0]["confidence"],
                base_score=base,
                evidence=[],
                rejected_candidates=rejected,
                summary=summary,
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_cat, best_base, best_hits, desc = scored[0]
        for other_score, other_cat, _, _, _ in scored[1:]:
            rejected.append(
                {
                    "candidate": other_cat,
                    "confidence": round(other_score, 4),
                    "reasons": [f"Outranked by {best_cat} ({best_score:.2f})"],
                }
            )
        return Classification(
            root_cause=best_cat,
            confidence=best_score,
            base_score=best_base,
            evidence=best_hits,
            rejected_candidates=rejected,
            summary=desc,
        )
