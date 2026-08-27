from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from debate import DebateController, load_settings


class ControllerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.harness = self.root / "harness"
        self.bin_dir = self.root / "bin"
        self.project.mkdir()
        self.harness.mkdir()
        self.bin_dir.mkdir()
        (self.project / "evidence.txt").write_text("fixture", encoding="utf-8")
        for name, text in {
            "task.md": "Produce a deterministic candidate.",
            "Prometheus.md": "You are Prometheus.",
            "Momus.md": "You are Momus.",
        }.items():
            (self.harness / name).write_text(text, encoding="utf-8")

        repository = Path(__file__).resolve().parents[1]
        shutil.copy2(repository / "schema.json", self.harness / "schema.json")
        shutil.copy2(
            repository / "adjudication_schema.json",
            self.harness / "adjudication_schema.json",
        )
        fake = self.bin_dir / "codex"
        shutil.copy2(Path(__file__).parent / "fake_codex.py", fake)
        fake.chmod(0o755)

        self.config = self.harness / "config.ini"
        self.config.write_text(
            f"""
[debate]
min_counter_rounds = 1
max_counter_rounds = 2
blind_second_agent = false
final_acceptance_audit = false
max_protocol_repairs = 1
heartbeat_seconds = 1
turn_timeout_minutes = 1

[codex]
model =
reasoning_effort =
web_search = inherit
sandbox = read-only
skip_git_repo_check = true
ignore_user_config = true
ignore_rules = true

[paths]
project_root = {self.project}
task_file = task.md
prometheus_file = Prometheus.md
momus_file = Momus.md
schema_file = schema.json
adjudication_schema_file = adjudication_schema.json
runs_dir = runs
state_dir = state

[isolation]
enabled = false
backend = auto
extra_read_paths =

[budget]
max_model_calls = 12
max_wall_minutes = 5
max_total_tokens = 1000
max_estimated_cost_usd = 0
input_usd_per_million = 0
cached_input_usd_per_million = 0
output_usd_per_million = 0

[evidence]
require_for_acceptance = true

[adjudication]
mode = human
model =
reasoning_effort = high

[output]
publish_prompts = true
publish_raw_jsonl = true
keep_private_runtime_on_success = false
keep_private_runtime_on_failure = true
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config, None)
        self.counter = self.root / "counter"
        self.counter.write_text("0", encoding="utf-8")
        self.fail_marker = self.root / "fail-once"
        self.environment = {
            **os.environ,
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_CODEX_COUNTER": str(self.counter),
            "FAKE_CODEX_FAIL_MARKER": str(self.fail_marker),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def localize_controller_files(self, controller):
        controller.status_file = self.harness / "RUN_STATUS.json"
        controller.latest_file = self.harness / "LATEST_RUN.txt"
        controller.lock_path = self.harness / ".debate.lock"

    def test_none_mode_publishes_unadjudicated_consensus(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace(
                "mode = human",
                "mode = none",
            ),
            encoding="utf-8",
        )
        settings = load_settings(self.config, None)
        environment = dict(self.environment)
        environment.pop("FAKE_CODEX_FAIL_MARKER")

        with patch.dict(os.environ, environment, clear=True):
            controller = DebateController(settings)
            self.localize_controller_files(controller)
            run_id = controller.run_id
            controller.run()

        archive = settings.runs_dir / run_id
        report = archive / "CONSENSUS_UNADJUDICATED.md"
        self.assertTrue(report.is_file())
        self.assertIn(
            "WITHOUT INDEPENDENT ADJUDICATION",
            report.read_text(encoding="utf-8"),
        )
        self.assertFalse((archive / "CONSENSUS.md").exists())
        self.assertFalse((archive / "ADJUDICATION.json").exists())
        self.assertFalse((archive / "ADJUDICATION_REQUEST.md").exists())
        self.assertFalse((archive / "adjudication_request.json").exists())
        self.assertFalse((archive / "adjudication_template.json").exists())
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "4")

        manifest = json.loads(
            (archive / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["outcome"], "consensus_unadjudicated")
        self.assertEqual(manifest["adjudication_mode"], "none")
        checkpoint = json.loads(
            (archive / "checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["phase"], "terminal")
        self.assertEqual(checkpoint["outcome"], "consensus_unadjudicated")
        self.assertIsNone(checkpoint["adjudication"])
        self.assertFalse((settings.state_dir / run_id).exists())

    def test_missing_adjudication_section_defaults_to_none(self):
        block = """[adjudication]
mode = human
model =
reasoning_effort = high

"""
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace(block, ""),
            encoding="utf-8",
        )
        settings = load_settings(self.config, None)
        self.assertEqual(settings.adjudication_mode, "none")

    def test_interruption_requires_acknowledgement_then_human_gate(self):
        with patch.dict(os.environ, self.environment, clear=True):
            controller = DebateController(self.settings)
            self.localize_controller_files(controller)
            run_id = controller.run_id
            with self.assertRaisesRegex(RuntimeError, "exit code 23"):
                controller.run()

            checkpoint = json.loads(
                (
                    self.settings.state_dir / run_id / "checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["phase"], "debating")
            self.assertIsNotNone(checkpoint["inflight"])
            status = json.loads(
                (self.harness / "RUN_STATUS.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("private_transcript", status)

            with self.assertRaisesRegex(RuntimeError, "--retry-inflight"):
                DebateController(self.settings, resume_id=run_id)

            resumed = DebateController(
                self.settings,
                resume_id=run_id,
                retry_inflight=True,
            )
            self.localize_controller_files(resumed)
            resumed.run()
            checkpoint = json.loads(
                (
                    self.settings.state_dir / run_id / "checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["phase"], "pending_adjudication")
            self.assertFalse((self.settings.runs_dir / run_id).exists())

            review = self.root / "review.json"
            review.write_text(
                json.dumps(
                    {
                        "decision": "APPROVE",
                        "rationale": "Fixture independently reviewed.",
                        "blocking_issues": [],
                        "evidence_checks": [
                            {
                                "source": "evidence.txt",
                                "result": "verified",
                                "notes": "Fixture content checked independently.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            adjudicator = DebateController(self.settings, resume_id=run_id)
            self.localize_controller_files(adjudicator)
            with patch.object(
                adjudicator,
                "publish_run",
                side_effect=RuntimeError("simulated publication crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "publication crash"):
                    adjudicator.adjudicate(
                        review,
                        reviewer="integration-test",
                    )
            publishing_checkpoint = json.loads(
                (
                    self.settings.state_dir / run_id / "checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(publishing_checkpoint["phase"], "publishing")

            publisher = DebateController(self.settings, resume_id=run_id)
            self.localize_controller_files(publisher)
            publisher.run()

            archive = self.settings.runs_dir / run_id
            self.assertTrue((archive / "CONSENSUS.md").is_file())
            self.assertTrue((archive / "ADJUDICATION.json").is_file())
            archived_checkpoint = json.loads(
                (archive / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(archived_checkpoint["phase"], "terminal")
            self.assertEqual(
                archived_checkpoint["adjudication"]["reviewer"],
                "human:integration-test",
            )
            self.assertFalse((self.settings.state_dir / run_id).exists())


if __name__ == "__main__":
    unittest.main()
