import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hermes_sre.cli import main

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


class TestCLI(unittest.TestCase):
    def test_cli_benchmark_markdown(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["benchmark", "--scenarios-dir", str(SCENARIOS_DIR), "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("6/6", buf.getvalue())

    def test_cli_inventory_diagnose_heal_and_status(self) -> None:
        scenario = str(SCENARIOS_DIR / "01-true-oomkilled.json")
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Inventory
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([
                    "--state-dir", tmp,
                    "inventory", "--namespace", "apps", "--env", "staging",
                    "--scenario", scenario, "--format", "json",
                ])
            self.assertEqual(code, 0)
            inv = json.loads(buf.getvalue())
            self.assertEqual(inv["healthy_count"], 1)
            self.assertEqual(inv["abnormal_count"], 1)
            self.assertEqual(inv["succeeded_count"], 1)

            # 2. Diagnose
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([
                    "--state-dir", tmp,
                    "diagnose", "--namespace", "apps", "--env", "staging",
                    "--pod", "checkout-api-7f8b9c-x4k2p", "--scenario", scenario,
                ])
            self.assertEqual(code, 0)
            diag = json.loads(buf.getvalue())
            self.assertEqual(diag["classification"]["root_cause"], "OOM_KILLED")
            self.assertEqual(diag["observation"]["redacted_count"], 1)

            # 3. Heal (--apply) + Status
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([
                    "--state-dir", tmp,
                    "heal", "--namespace", "apps", "--env", "staging",
                    "--pod", "checkout-api-7f8b9c-x4k2p", "--scenario", scenario,
                    "--request-id", "cli-test-1", "--apply",
                ])
            self.assertEqual(code, 0)
            healed = json.loads(buf.getvalue())
            self.assertEqual(healed["lifecycle_state"], "resolved")

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--state-dir", tmp, "status", "--request-id", "cli-test-1"])
            self.assertEqual(code, 0)
            receipt = json.loads(buf.getvalue())
            self.assertEqual(receipt["idempotency_key"], healed["receipt"]["idempotency_key"])


if __name__ == "__main__":
    unittest.main()
