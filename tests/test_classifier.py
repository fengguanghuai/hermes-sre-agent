import unittest

from hermes_sre.classifier import Classifier
from hermes_sre.models import Observation, PodSnapshot
from hermes_sre.observer import classify_inventory_pods, redact_lines
from hermes_sre.policy import PolicyBundle


class TestClassifierAndObserver(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = PolicyBundle.load()
        self.classifier = Classifier(self.policies)

    def test_negative_evidence_blocks_startup_probe_false_oom(self) -> None:
        pod = PodSnapshot(
            namespace="apps",
            name="search-worker-1",
            uid="u-1",
            deployment="search-worker",
            container_name="worker",
            phase="Running",
            ready=False,
            reason="CrashLoopBackOff",
            exit_code=137,
            last_state_reason="OOMKilled",
            memory_limit_mi=2048,
        )
        obs = Observation(
            namespace="apps",
            environment="staging",
            pod=pod,
            events=["Unhealthy: Startup probe failed: context deadline exceeded"],
            logs=["WARN Received SIGKILL before listen"],
        )
        result = self.classifier.classify(obs)
        self.assertEqual(result.root_cause, "UNKNOWN")
        self.assertLess(result.confidence, 0.55)
        self.assertTrue(
            any(c["candidate"] == "OOM_KILLED" for c in result.rejected_candidates)
        )

    def test_negative_evidence_blocks_manifest_unknown_image_pull(self) -> None:
        pod = PodSnapshot(
            namespace="apps",
            name="api-1",
            uid="u-2",
            deployment="api",
            container_name="app",
            phase="Pending",
            ready=False,
            reason="ImagePullBackOff",
        )
        obs = Observation(
            namespace="apps",
            environment="staging",
            pod=pod,
            events=["Failed: rpc error: code = NotFound desc = manifest unknown"],
            logs=[],
        )
        result = self.classifier.classify(obs)
        self.assertEqual(result.root_cause, "UNKNOWN")
        self.assertTrue(
            any(
                c["candidate"] == "TRANSIENT_IMAGE_PULL"
                for c in result.rejected_candidates
            )
        )

    def test_secret_redaction_masks_tokens_and_dsn_passwords(self) -> None:
        lines = [
            "INFO auth token=eyJhbGciOiJIUzI1NiJ9.payload",
            "INFO db=postgres://admin:supersecret@db.internal:5432/app",
        ]
        cleaned, count = redact_lines(lines)
        self.assertEqual(count, 2)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", cleaned[0])
        self.assertNotIn("supersecret", cleaned[1])
        self.assertIn("[REDACTED]", cleaned[0])
        self.assertIn("[REDACTED]", cleaned[1])

    def test_inventory_hygiene_separates_transitioning_succeeded_and_healthy(self) -> None:
        pods = [
            PodSnapshot("apps", "healthy-restarted", "1", "a", "c", "Running", True, restarts=3, last_state_reason="OOMKilled", age_seconds=9000),
            PodSnapshot("apps", "starting-new", "2", "b", "c", "Pending", False, age_seconds=60),
            PodSnapshot("apps", "terminating-ok", "3", "c", "c", "Running", False, terminating_seconds=120),
            PodSnapshot("apps", "terminating-stuck", "4", "d", "c", "Running", False, terminating_seconds=900),
            PodSnapshot("apps", "job-done", "5", "e", "c", "Succeeded", False, age_seconds=500),
            PodSnapshot("apps", "crashing", "6", "f", "c", "Running", False, reason="CrashLoopBackOff", age_seconds=40),
        ]
        summary = classify_inventory_pods("apps", "staging", pods)
        self.assertEqual(summary.healthy_count, 1)
        self.assertEqual([p.name for p in summary.succeeded], ["job-done"])
        self.assertEqual(
            sorted(p.name for p in summary.transitioning),
            ["starting-new", "terminating-ok"],
        )
        self.assertEqual(
            sorted(p.name for p in summary.abnormal),
            ["crashing", "terminating-stuck"],
        )


if __name__ == "__main__":
    unittest.main()
