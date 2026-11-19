"""Declarative policy bundle loader for confidence, healing, and cooldown rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yaml_loader import load_structured_file


@dataclass(frozen=True)
class PolicyBundle:
    confidence: dict[str, Any]
    heal: dict[str, Any]
    cooldown: dict[str, Any]

    @classmethod
    def load(cls, policy_dir: Path | None = None) -> "PolicyBundle":
        base = policy_dir or default_policy_dir()
        confidence = load_structured_file(base / "confidence_policy.yaml")
        heal = load_structured_file(base / "heal_policy.yaml")
        cooldown = load_structured_file(base / "cooldown_policy.yaml")
        for name, data, required in (
            ("confidence_policy.yaml", confidence, ("thresholds", "categories")),
            ("heal_policy.yaml", heal, ("policies", "env_constraints")),
            ("cooldown_policy.yaml", cooldown, ("cooldowns",)),
        ):
            for key in required:
                if key not in data:
                    raise ValueError(f"Missing required key {key!r} in {name}")
        return cls(confidence=confidence, heal=heal, cooldown=cooldown)


def default_policy_dir() -> Path:
    # Repo layout: <root>/src/hermes_sre/policy.py -> <root>/policies
    repo_candidate = Path(__file__).resolve().parents[2] / "policies"
    if repo_candidate.is_dir():
        return repo_candidate
    cwd_candidate = Path.cwd() / "policies"
    if cwd_candidate.is_dir():
        return cwd_candidate
    raise FileNotFoundError(
        "Could not locate policies/ directory. Pass --policy-dir explicitly."
    )
