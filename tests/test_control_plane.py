import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_sre.control_plane import ControlPlane
from hermes_sre.models import Classification, Observation, PodSnapshot
from hermes_sre.policy import PolicyBundle


def _make_obs(
    *,
    env: str = "staging",
    pod_name: str = "orders-1",
    deployment: str = "orders",
    memory_limit_mi: int | None = 512,
    node_allocatable_mi: int | None = 16384,
    quota_remaining_mi: int | None = 8192,
    logs: list[str] | None = None,
) -> Observation:
    return Observation(
        namespace="apps",
        environment=env,
        pod=PodSnapshot(
            namespace="apps",
            name=pod_name,
            uid=f"uid-{pod_name}",
            deployment=deployment,
            container_name="app",
            phase="Running",
            ready=False,
            reason="CrashLoopBackOff",
            memory_limit_mi=memory_limit_mi,
            node_allocatable_memory_mi=node_allocatable_mi,
            namespace_quota_remaining_mi=quota_remaining_mi,
        ),
        events=[],
        logs=logs or [],
    )


class TestControlPlane(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = PolicyBundle.load()
        self.tmp = tempfile.TemporaryDirectory()
        self.cp = ControlPlane(self.policies, Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prod_requires_approval_and_upgrades_risk(self) -> None:
        obs = _make_obs(env="prod")
        cls = Classification(root_cause="OOM_KILLED", confidence=0.95, base_score=0.85)
        blocked = self.cp.evaluate(obs, cls, approved=False)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.blocked_by, "approval_required")
        self.assertEqual(blocked.risk, "medium")  # low upgraded -> medium in prod

        approved = self.cp.evaluate(obs, cls, approved=True)
        self.assertTrue(approved.allowed)
        self.assertEqual(approved.target_memory_mi, 768)

    def test_four_layer_cooldown_circuit_breaker(self) -> None:
        now = datetime.now(timezone.utc)
        mount_cls = Classification(root_cause="TRANSIENT_MOUNT", confidence=0.92, base_score=0.82)
        pull_cls = Classification(root_cause="TRANSIENT_IMAGE_PULL", confidence=0.90, base_score=0.80)

        # Record 1st heal on orders-1 (TRANSIENT_MOUNT)
        self.cp.cooldown_store.record(
            namespace="apps",
            environment="staging",
            deployment="orders",
            pod_name="orders-1",
            pod_uid="uid-orders-1",
            root_cause="TRANSIENT_MOUNT",
            action="recreate_pod",
            now=now,
        )

        # Layer 1: same pod + same root
        d1 = self.cp.evaluate(_make_obs(pod_name="orders-1"), mount_cls, now=now)
        self.assertFalse(d1.allowed)
        self.assertEqual(d1.cooldown_layer, "same_pod_same_root")

        # Layer 2: same pod + different root
        d2 = self.cp.evaluate(_make_obs(pod_name="orders-1"), pull_cls, now=now)
        self.assertFalse(d2.allowed)
        self.assertEqual(d2.cooldown_layer, "same_pod_any_root")

        # Layer 3: different pod in same deployment
        d3 = self.cp.evaluate(_make_obs(pod_name="orders-2", deployment="orders"), mount_cls, now=now)
        self.assertFalse(d3.allowed)
        self.assertEqual(d3.cooldown_layer, "same_deployment")

        # Layer 4: cluster pressure panic (>=5 actions in 1m)
        for i in range(4):
            self.cp.cooldown_store.record(
                namespace="apps",
                environment="staging",
                deployment=f"other-{i}",
                pod_name=f"other-{i}-pod",
                pod_uid=f"uid-{i}",
                root_cause="TRANSIENT_MOUNT",
                action="recreate_pod",
                now=now,
            )
        d4 = self.cp.evaluate(_make_obs(pod_name="fresh-pod", deployment="fresh-svc"), mount_cls, now=now)
        self.assertFalse(d4.allowed)
        self.assertEqual(d4.cooldown_layer, "cluster_pressure")
        self.assertEqual(d4.risk, "critical")

    def test_memory_bounds_enforce_ceiling_quota_and_no_infinite_resize(self) -> None:
        cls = Classification(root_cause="OOM_KILLED", confidence=0.95, base_score=0.85)

        # At ceiling
        at_ceil = self.cp.evaluate(_make_obs(memory_limit_mi=16384), cls)
        self.assertFalse(at_ceil.allowed)
        self.assertEqual(at_ceil.blocked_by, "memory_bounds")

        # Exceeds namespace quota
        over_quota = self.cp.evaluate(_make_obs(memory_limit_mi=1024, quota_remaining_mi=128), cls)
        self.assertFalse(over_quota.allowed)
        self.assertEqual(over_quota.blocked_by, "memory_bounds")

    def test_preflight_log_guard_blocks_recreate_on_deterministic_config_error(self) -> None:
        obs = _make_obs(logs=["Could not resolve placeholder 'db.host'"])
        cls = Classification(root_cause="TRANSIENT_MOUNT", confidence=0.90, base_score=0.82)
        dec = self.cp.evaluate(obs, cls)
        self.assertFalse(dec.allowed)
        self.assertEqual(dec.blocked_by, "preflight_log_guard")


if __name__ == "__main__":
    unittest.main()
