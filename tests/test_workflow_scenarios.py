import tempfile
import unittest
from pathlib import Path

from hermes_sre.policy import PolicyBundle
from hermes_sre.providers.replay import ScenarioReplayProvider
from hermes_sre.workflow import SREWorkflow, run_benchmark


SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


class TestWorkflowScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = PolicyBundle.load()

    def test_all_six_scenarios_pass_benchmark(self) -> None:
        result = run_benchmark(SCENARIOS_DIR, self.policies)
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["passed"], 6, msg=f"Failed scenarios: {result}")
        self.assertEqual(result["pass_rate"], 100.0)

    def test_dry_run_does_not_mutate_and_idempotency_blocks_duplicate_request(self) -> None:
        scenario_path = SCENARIOS_DIR / "01-true-oomkilled.json"
        provider = ScenarioReplayProvider.from_file(scenario_path)
        with tempfile.TemporaryDirectory() as tmp:
            wf = SREWorkflow(provider, self.policies, Path(tmp))
            dry = wf.heal(
                namespace="apps",
                environment="staging",
                pod_name="checkout-api-7f8b9c-x4k2p",
                request_id="req-dry-001",
                apply=False,
            )
            self.assertEqual(dry.receipt.mode, "dry_run")
            self.assertEqual(dry.lifecycle_state, "investigating")
            self.assertEqual(len(provider.applied_mutations), 0)

            # Replaying same request_id must be rejected cleanly
            with self.assertRaises(RuntimeError):
                wf.heal(
                    namespace="apps",
                    environment="staging",
                    pod_name="checkout-api-7f8b9c-x4k2p",
                    request_id="req-dry-001",
                    apply=True,
                )

            # Status lookup returns persisted receipt
            stored = wf.status("req-dry-001")
            self.assertIsNotNone(stored)
            self.assertEqual(stored["mode"], "dry_run")


if __name__ == "__main__":
    unittest.main()
