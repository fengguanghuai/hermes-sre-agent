"""Stage 5: Post-Action Reconciliation and Idempotent Receipt Store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ActionReceipt
from .providers.base import ClusterProvider


class ReceiptStore:
    """Persists execution receipts by request_id so callers inspect status instead of replaying."""

    def __init__(self, state_dir: Path) -> None:
        self.receipts_dir = state_dir / "receipts"

    def path_for(self, request_id: str) -> Path:
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request_id)
        return self.receipts_dir / f"{safe_id}.json"

    def save(self, receipt: ActionReceipt) -> Path:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(receipt.request_id)
        path.write_text(
            json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def load(self, request_id: str) -> dict[str, Any] | None:
        path = self.path_for(request_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


class Reconciler:
    """Verifies replacement pod readiness after mutation (mitigated != resolved)."""

    def __init__(self, provider: ClusterProvider, state_dir: Path) -> None:
        self.provider = provider
        self.store = ReceiptStore(state_dir)

    def reconcile(self, receipt: ActionReceipt) -> ActionReceipt:
        if receipt.mode != "applied":
            self.store.save(receipt)
            return receipt

        outcome = self.provider.verify_reconciliation(
            namespace=receipt.namespace,
            deployment=receipt.deployment,
            old_pod_uid=receipt.pod_uid,
            action=receipt.action,
        )
        receipt.reconciliation = outcome
        receipt.lifecycle_state = "resolved" if outcome.get("verified") else "mitigated"
        self.store.save(receipt)
        return receipt
