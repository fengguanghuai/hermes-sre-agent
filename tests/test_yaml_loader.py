import unittest
from pathlib import Path

from hermes_sre.policy import PolicyBundle, default_policy_dir
from hermes_sre.yaml_loader import parse_simple_yaml


class TestYamlLoader(unittest.TestCase):
    def test_parse_all_policy_files_with_stdlib_parser(self) -> None:
        policy_dir = default_policy_dir()
        for filename in (
            "confidence_policy.yaml",
            "heal_policy.yaml",
            "cooldown_policy.yaml",
        ):
            raw = (policy_dir / filename).read_text(encoding="utf-8")
            parsed = parse_simple_yaml(raw)
            self.assertIsInstance(parsed, dict)
            self.assertEqual(parsed["version"], "1.0.0")

    def test_policy_bundle_loads_expected_sections(self) -> None:
        bundle = PolicyBundle.load()
        self.assertIn("OOM_KILLED", bundle.confidence["categories"])
        self.assertEqual(bundle.heal["env_constraints"]["prod"]["require_approval"], True)
        self.assertEqual(bundle.heal["env_constraints"]["prod"]["cooldown_multiplier"], 3)
        self.assertEqual(
            bundle.cooldown["cooldowns"]["cluster_pressure"]["max_actions"], 5
        )


if __name__ == "__main__":
    unittest.main()
