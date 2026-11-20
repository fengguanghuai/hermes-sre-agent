import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_sre.policy import PolicyBundle
from hermes_sre.providers.docker import DockerProvider, snapshot_from_inspect
from hermes_sre.workflow import SREWorkflow

CID = "a" * 64


def _container(
    *,
    status: str = "running",
    oom: bool = False,
    exit_code: int = 0,
    health: str | None = None,
    memory_mi: int = 512,
    project: str | None = "shop",
) -> dict:
    labels = {"com.docker.compose.project": project, "com.docker.compose.service": "orders"} if project else {}
    state = {
        "Status": status,
        "OOMKilled": oom,
        "ExitCode": exit_code,
        "StartedAt": "2020-01-01T00:00:00.123456789Z",
    }
    if health:
        state["Health"] = {
            "Status": health,
            "Log": [{"ExitCode": 1, "Output": "curl: (7) Failed to connect to localhost port 8080"}],
        }
    return {
        "Id": CID,
        "Name": "/shop-orders-1",
        "RestartCount": 3,
        "State": state,
        "Config": {"Labels": labels},
        "HostConfig": {"Memory": memory_mi * 1024 * 1024},
    }


class FakeDocker:
    def __init__(self, container: dict, logs: str = "", events: list[dict] | None = None) -> None:
        self.container = container
        self.logs = logs
        self.events = events or []
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        args = cmd[1:]
        self.calls.append(args)
        out = ""
        if args[0] == "info":
            out = str(8192 * 1024 * 1024)
        elif args[0] == "ps":
            out = CID
        elif args[0] == "inspect":
            out = json.dumps([self.container])
        elif args[0] == "logs":
            out = self.logs
        elif args[0] == "events":
            out = "\n".join(json.dumps(e) for e in self.events)
        elif args[0] in ("update", "start", "restart"):
            # After a mutation the container comes back healthy.
            self.container = _container(memory_mi=768)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def mutations(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] in ("update", "start", "restart")]


def _workflow(fake: FakeDocker, tmp: str) -> SREWorkflow:
    provider = DockerProvider(runner=fake, reconcile_timeout_seconds=1, reconcile_interval_seconds=0)
    return SREWorkflow(provider, PolicyBundle.load(), Path(tmp))


class TestDockerSnapshot(unittest.TestCase):
    def test_state_mapping(self) -> None:
        cases = [
            (_container(), "Running", True, ""),
            (_container(status="restarting", oom=True, exit_code=137), "Running", False, "OOMKilled"),
            (_container(status="restarting", exit_code=1), "Running", False, "CrashLoopBackOff"),
            (_container(status="exited", exit_code=1), "Failed", False, "Error"),
            (_container(status="exited", exit_code=0), "Succeeded", False, ""),
            (_container(health="unhealthy"), "Running", False, "Unhealthy"),
        ]
        for item, phase, ready, reason in cases:
            snap = snapshot_from_inspect(item, 8192)
            self.assertEqual((snap.phase, snap.ready, snap.reason), (phase, ready, reason), item["State"])

    def test_compose_labels_and_memory(self) -> None:
        snap = snapshot_from_inspect(_container(memory_mi=512), 8192)
        self.assertEqual(snap.namespace, "shop")
        self.assertEqual(snap.deployment, "orders")
        self.assertEqual(snap.memory_limit_mi, 512)
        self.assertEqual(snap.node_allocatable_memory_mi, 8192)
        self.assertEqual(snapshot_from_inspect(_container(project=None)).namespace, "standalone")


class TestDockerWorkflow(unittest.TestCase):
    def test_inventory_lists_abnormal_container(self) -> None:
        fake = FakeDocker(_container(status="exited", exit_code=1))
        with tempfile.TemporaryDirectory() as tmp:
            summary = _workflow(fake, tmp).inventory("shop", "staging")
        self.assertEqual([p.name for p in summary.abnormal], ["shop-orders-1"])

    def test_oom_dry_run_then_apply_resizes_by_container_id(self) -> None:
        fake = FakeDocker(
            _container(status="restarting", oom=True, exit_code=137),
            events=[{"Action": "oom"}, {"Action": "die", "Actor": {"Attributes": {"exitCode": "137"}}}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            wf = _workflow(fake, tmp)
            dry = wf.heal(namespace="all", environment="staging", pod_name="shop-orders-1", request_id="d1")
            self.assertEqual(dry.classification.root_cause, "OOM_KILLED")
            self.assertEqual(dry.decision.target_memory_mi, 768)
            self.assertEqual(fake.mutations(), [])
            self.assertEqual(dry.observation.namespace, "shop")

            applied = wf.heal(namespace="all", environment="staging", pod_name="shop-orders-1", request_id="a1", apply=True)
        self.assertEqual(fake.mutations()[0], ["update", "--memory", "768m", "--memory-swap", "768m", CID])
        self.assertEqual(applied.lifecycle_state, "resolved")
        self.assertIn("persist_hint", applied.receipt.details)

    def test_missing_config_is_not_restarted_and_user_is_asked(self) -> None:
        fake = FakeDocker(
            _container(status="restarting", exit_code=1),
            logs="Error: Missing required environment variable: PAYMENT_API_KEY\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = _workflow(fake, tmp).heal(
                namespace="shop", environment="staging", pod_name="shop-orders-1", request_id="c1", apply=True
            )
        self.assertEqual(report.classification.root_cause, "CONFIG_MISSING")
        self.assertEqual(fake.mutations(), [])
        self.assertIn("PAYMENT_API_KEY", report.next_steps["summary"])

    def test_unhealthy_without_known_cause_asks_user(self) -> None:
        fake = FakeDocker(_container(health="unhealthy"))
        with tempfile.TemporaryDirectory() as tmp:
            report = _workflow(fake, tmp).heal(
                namespace="shop", environment="staging", pod_name="shop-orders-1", request_id="u1", apply=True
            )
        self.assertFalse(report.decision.allowed)
        self.assertEqual(fake.mutations(), [])
        self.assertTrue(report.next_steps["needs_user_input"])
        self.assertTrue(any("Health check failed" in e for e in report.observation.events))


if __name__ == "__main__":
    unittest.main()
