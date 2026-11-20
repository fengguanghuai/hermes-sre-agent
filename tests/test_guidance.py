import tempfile
import unittest
from pathlib import Path

from hermes_sre.policy import PolicyBundle
from hermes_sre.providers.replay import ScenarioReplayProvider
from hermes_sre.workflow import SREWorkflow, render_incident_markdown

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


def _run(scenario: str, pod: str, *, env: str = "staging", apply: bool = False, approved: bool = False):
    provider = ScenarioReplayProvider.from_file(SCENARIOS_DIR / scenario)
    with tempfile.TemporaryDirectory() as tmp:
        wf = SREWorkflow(provider, PolicyBundle.load(), Path(tmp))
        return wf.heal(
            namespace="apps",
            environment=env,
            pod_name=pod,
            request_id="guidance-test",
            apply=apply,
            approved=approved,
        )


class TestGuidance(unittest.TestCase):
    def test_config_missing_names_the_key_and_asks_user(self) -> None:
        report = _run("03-config-missing-block.json", "notification-svc-5b7d8f-m3n2q", apply=True)
        steps = report.next_steps
        self.assertTrue(steps["needs_user_input"])
        self.assertIn("service.webhook.url", steps["summary"])
        self.assertIn("### Need From You", render_incident_markdown(report))

    def test_code_defect_names_exception(self) -> None:
        report = _run("05-code-defect-dispatch.json", "billing-engine-4a2e6f-k9l0m", apply=True)
        self.assertIn("NoClassDefFoundError", report.next_steps["summary"])
        self.assertTrue(report.next_steps["needs_user_input"])

    def test_low_confidence_asks_for_dependencies_and_probe_info(self) -> None:
        report = _run("02-probe-false-oom.json", "search-worker-6d5c4b-9p8q1", apply=True)
        questions = " ".join(q["question"] for q in report.next_steps["ask_user"])
        self.assertIn("start", questions)
        self.assertTrue(any("--dep" in q.get("how_to_supply", "") for q in report.next_steps["ask_user"]))

    def test_dry_run_asks_for_confirmation(self) -> None:
        report = _run("01-true-oomkilled.json", "checkout-api-7f8b9c-x4k2p")
        ask = report.next_steps["ask_user"]
        self.assertEqual(len(ask), 1)
        self.assertIn("--apply", ask[0]["how_to_supply"])

    def test_prod_asks_for_approval(self) -> None:
        report = _run("01-true-oomkilled.json", "checkout-api-7f8b9c-x4k2p", env="prod", apply=True)
        self.assertEqual(report.decision.blocked_by, "approval_required")
        self.assertIn("--approve", report.next_steps["ask_user"][0]["how_to_supply"])

    def test_resolved_needs_no_input(self) -> None:
        report = _run("01-true-oomkilled.json", "checkout-api-7f8b9c-x4k2p", apply=True)
        self.assertEqual(report.lifecycle_state, "resolved")
        self.assertFalse(report.next_steps["needs_user_input"])


if __name__ == "__main__":
    unittest.main()
