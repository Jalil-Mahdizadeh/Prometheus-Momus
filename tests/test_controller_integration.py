from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from debate import DebateController, load_settings
from controller_safety import CheckpointStore


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

    def configure(self, *changes):
        text = self.config.read_text(encoding="utf-8")
        for before, after in changes:
            text = text.replace(before, after)
        self.config.write_text(text, encoding="utf-8")
        self.settings = load_settings(self.config, None)

    def scripted_environment(self, steps):
        script = self.root / "script.json"
        script.write_text(json.dumps(steps), encoding="utf-8")
        self.counter.write_text("0", encoding="utf-8")
        environment = dict(self.environment)
        environment.pop("FAKE_CODEX_FAIL_MARKER", None)
        environment["FAKE_CODEX_SCRIPT"] = str(script)
        environment["FAKE_CODEX_CALL_LOG"] = str(self.root / "calls.jsonl")
        return environment

    def new_controller(self, **kwargs):
        controller = DebateController(self.settings, **kwargs)
        self.localize_controller_files(controller)
        return controller

    @staticmethod
    def evidence(source="evidence.txt", status="verified"):
        return {
            "claim": "The fixture exists.", "source": source,
            "source_type": "project_file", "status": status,
            "notes": "Fixture evidence.",
        }

    @staticmethod
    def review(sources):
        return {
            "decision": "APPROVE", "rationale": "Independently checked fixtures.",
            "blocking_issues": [],
            "evidence_checks": [
                {"source": source, "result": "verified", "notes": "Checked fixture."}
                for source in sources
            ],
        }

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

    def test_no_consensus_can_be_continued_repeatedly(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace(
                "mode = human",
                "mode = none",
            ),
            encoding="utf-8",
        )
        settings = load_settings(self.config, None)
        counter_environment = dict(self.environment)
        counter_environment.pop("FAKE_CODEX_FAIL_MARKER")
        counter_environment["FAKE_CODEX_ALWAYS_COUNTER"] = "1"

        with patch.dict(os.environ, counter_environment, clear=True):
            controller = DebateController(settings)
            self.localize_controller_files(controller)
            run_id = controller.run_id
            controller.run()

        base_archive = settings.runs_dir / run_id
        base_report = base_archive / "NO_CONSENSUS.md"
        original_report = base_report.read_text(encoding="utf-8")
        self.assertIn("--extra-rounds N", original_report)
        self.assertTrue((settings.state_dir / run_id).is_dir())

        base_checkpoint = json.loads(
            (base_archive / "checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertEqual(base_checkpoint["outcome"], "no_consensus")
        self.assertEqual(base_checkpoint["completed_rounds"], 2)
        self.assertEqual(base_checkpoint["round_limit"], 2)
        self.assertEqual(base_checkpoint["continuation_index"], 0)
        base_threads = base_checkpoint["threads"]
        self.assertTrue(all(base_threads.values()))
        self.assertEqual(base_checkpoint["budget"]["calls"], 4)
        self.assertTrue(base_checkpoint["resumable"])
        with patch.dict(os.environ, counter_environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "--extra-rounds"):
                DebateController(
                    settings,
                    resume_id=run_id,
                )

        with patch.dict(os.environ, counter_environment, clear=True):
            continued = DebateController(
                settings,
                resume_id=run_id,
                extra_rounds=2,
            )
            self.localize_controller_files(continued)
            continued.run()

        first_continuation = (
            settings.runs_dir / f"{run_id}-continuation-1"
        )
        first_checkpoint = json.loads(
            (first_continuation / "checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertTrue((first_continuation / "NO_CONSENSUS.md").is_file())
        self.assertEqual(first_checkpoint["completed_rounds"], 4)
        self.assertEqual(first_checkpoint["round_limit"], 4)
        self.assertEqual(first_checkpoint["continuation_index"], 1)
        self.assertEqual(first_checkpoint["threads"], base_threads)
        self.assertEqual(first_checkpoint["budget"]["calls"], 6)
        self.assertTrue(first_checkpoint["resumable"])
        self.assertEqual(base_report.read_text(encoding="utf-8"), original_report)
        self.assertTrue((settings.state_dir / run_id).is_dir())

        accepting_environment = dict(counter_environment)
        accepting_environment.pop("FAKE_CODEX_ALWAYS_COUNTER")
        with patch.dict(os.environ, accepting_environment, clear=True):
            continued_again = DebateController(
                settings,
                resume_id=run_id,
                extra_rounds=1,
            )
            self.localize_controller_files(continued_again)
            continued_again.run()

        final_archive = settings.runs_dir / f"{run_id}-continuation-2"
        final_checkpoint = json.loads(
            (final_archive / "checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            (final_archive / "CONSENSUS_UNADJUDICATED.md").is_file()
        )
        self.assertFalse((final_archive / "NO_CONSENSUS.md").exists())
        self.assertEqual(final_checkpoint["round_limit"], 5)
        self.assertEqual(final_checkpoint["continuation_index"], 2)
        self.assertEqual(len(final_checkpoint["continuations"]), 2)
        self.assertFalse(final_checkpoint["resumable"])
        self.assertEqual(final_checkpoint["threads"], base_threads)
        self.assertEqual(final_checkpoint["budget"]["calls"], 7)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "7")
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

    def test_alternating_repairs_cannot_bypass_minimum_rounds(self):
        self.configure(
            ("mode = human", "mode = none"),
            ("max_protocol_repairs = 1", "max_protocol_repairs = 2"),
        )
        environment = self.scripted_environment([
            {}, {},
            {"overrides": {"decision": "ACCEPT"}},
            {"overrides": {"decision": "PROPOSE"}},
            {"overrides": {"decision": "ACCEPT"}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            with self.assertRaisesRegex(RuntimeError, "minimum counter-rounds"):
                controller.run()
        checkpoint = controller.checkpoints.load()
        self.assertEqual(checkpoint["completed_rounds"], 0)
        self.assertEqual(checkpoint["budget"]["calls"], 5)
        self.assertFalse(controller.run_dir.exists())

    def test_repair_sequence_can_recover_to_counter_then_accept(self):
        self.configure(
            ("mode = human", "mode = none"),
            ("max_protocol_repairs = 1", "max_protocol_repairs = 3"),
            ("final_acceptance_audit = false", "final_acceptance_audit = true"),
        )
        environment = self.scripted_environment([
            {}, {},
            {"overrides": {"decision": "ACCEPT"}},
            {"overrides": {"decision": "PROPOSE"}},
            {"overrides": {"decision": "ACCEPT"}},
            {"overrides": {"decision": "COUNTER"}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            controller.run()
        checkpoint = CheckpointStore(controller.run_dir / "checkpoint.json").load()
        self.assertEqual(checkpoint["outcome"], "consensus_unadjudicated")
        self.assertEqual(checkpoint["completed_rounds"], 1)
        self.assertEqual(checkpoint["budget"]["calls"], 8)

    def test_acceptance_requires_a_clean_candidate(self):
        self.configure(("mode = human", "mode = none"))
        for overrides, error in (
            ({"blocking_issues": ["Unresolved defect"]}, "Current candidate has blocking"),
            ({"evidence": []}, "Current candidate is missing"),
            ({"evidence": [self.evidence(status="disputed")]}, "Current candidate has disputed"),
        ):
            with self.subTest(overrides=overrides):
                environment = self.scripted_environment([{}, {}, {"overrides": overrides}])
                with patch.dict(os.environ, environment, clear=True):
                    controller = self.new_controller()
                    with self.assertRaisesRegex(RuntimeError, error):
                        controller.run()
                self.assertFalse(controller.run_dir.exists())

    def test_acceptance_repair_rechecks_stage_decisions(self):
        self.configure(("mode = human", "mode = none"))
        for final_audit in (False, True):
            with self.subTest(final_audit=final_audit):
                if final_audit:
                    self.configure(("final_acceptance_audit = false", "final_acceptance_audit = true"))
                environment = self.scripted_environment(
                    [{}] * (4 if final_audit else 3) + [
                        {"overrides": {"evidence": []}},
                        {"overrides": {"decision": "PROPOSE"}},
                    ]
                )
                with patch.dict(os.environ, environment, clear=True):
                    controller = self.new_controller()
                    with self.assertRaisesRegex(RuntimeError, "decision 'PROPOSE' is invalid"):
                        controller.run()
                self.assertFalse(controller.run_dir.exists())

    def test_counter_can_correct_candidate_before_acceptance(self):
        self.configure(
            ("mode = human", "mode = none"),
            ("max_counter_rounds = 2", "max_counter_rounds = 3"),
        )
        environment = self.scripted_environment([
            {}, {}, {"overrides": {"blocking_issues": ["Unresolved defect"]}}, {},
            {"overrides": {"decision": "COUNTER", "proposal": "Corrected candidate"}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            controller.run()
        saved = CheckpointStore(controller.run_dir / "checkpoint.json").load()
        self.assertEqual(saved["outcome"], "consensus_unadjudicated")
        self.assertEqual(saved["completed_rounds"], 2)
        self.assertEqual(saved["active"]["proposal"], "Corrected candidate")
        self.assertEqual(saved["active"]["blocking_issues"], [])

    def test_final_audit_preserves_all_acceptance_evidence(self):
        self.configure(
            ("mode = human", "mode = none"),
            ("final_acceptance_audit = false", "final_acceptance_audit = true"),
        )
        for name in ("acceptance.txt", "audit.txt"):
            (self.project / name).write_text("support", encoding="utf-8")
        environment = self.scripted_environment([
            {}, {}, {},
            {"overrides": {"evidence": [self.evidence("acceptance.txt")]}},
            {"overrides": {"evidence": [self.evidence("audit.txt")]}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            controller.run()
        candidate = json.loads((controller.run_dir / "final_candidate.json").read_text())
        self.assertEqual(
            {item["source"] for item in candidate["evidence"]},
            {"evidence.txt", "acceptance.txt", "audit.txt"},
        )
        self.assertEqual(candidate["proposal"], "Improved standalone candidate")
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertIn("acceptance.txt", calls[-1]["prompt"])

    def test_human_gate_checks_acceptance_sources_too(self):
        (self.project / "acceptance.txt").write_text("support", encoding="utf-8")
        environment = self.scripted_environment([
            {}, {}, {},
            {"overrides": {"evidence": [self.evidence("acceptance.txt")]}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            controller.run()
            packet = json.loads((controller.private_dir / "adjudication_request.json").read_text())
            self.assertEqual(
                {item["source"] for item in packet["candidate"]["evidence"]},
                {"evidence.txt", "acceptance.txt"},
            )
            review_file = self.root / "review.json"
            review_file.write_text(json.dumps(self.review(["evidence.txt"])))
            judge = self.new_controller(resume_id=controller.run_id)
            with self.assertRaisesRegex(ValueError, "acceptance.txt"):
                judge.adjudicate(review_file, reviewer="test")
            self.assertFalse(controller.run_dir.exists())
            review_file.write_text(json.dumps(self.review(["evidence.txt", "acceptance.txt"])))
            judge = self.new_controller(resume_id=controller.run_id)
            judge.adjudicate(review_file, reviewer="test")
        self.assertTrue((controller.run_dir / "CONSENSUS.md").is_file())

    def test_model_gate_checks_acceptance_sources_too(self):
        self.configure(
            ("[codex]\nmodel =", "[codex]\nmodel = debate-fixture"),
            ("mode = human\nmodel =", "mode = model\nmodel = judge-fixture"),
        )
        (self.project / "acceptance.txt").write_text("support", encoding="utf-8")
        environment = self.scripted_environment([
            {}, {}, {},
            {"overrides": {"evidence": [self.evidence("acceptance.txt")]}},
            {"response": self.review(["evidence.txt"])},
            {"response": self.review(["evidence.txt", "acceptance.txt"])},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            with self.assertRaisesRegex(ValueError, "acceptance.txt"):
                controller.run()
            self.assertFalse(controller.run_dir.exists())
            resumed = self.new_controller(resume_id=controller.run_id, retry_inflight=True)
            resumed.run()
        self.assertTrue((controller.run_dir / "CONSENSUS.md").is_file())

    def test_pending_legacy_acceptance_is_revalidated(self):
        with patch.dict(os.environ, self.scripted_environment([]), clear=True):
            controller = self.new_controller()
            controller.run()
            saved = controller.checkpoints.load()
            saved["active"]["evidence"] = []
            controller.checkpoints.save(saved)
            judge = self.new_controller(resume_id=controller.run_id)
            with self.assertRaisesRegex(RuntimeError, "Current candidate is missing"):
                judge.adjudicate(
                    self._write_review_file(self.review([])), reviewer="test"
                )
        self.assertFalse(controller.run_dir.exists())

    def test_archive_checkpoint_write_is_atomic_and_recoverable(self):
        self.configure(("mode = human", "mode = none"))
        with patch.dict(os.environ, self.scripted_environment([]), clear=True):
            controller = self.new_controller()
            original_replace = os.replace

            def fail_archive_replace(source, destination):
                if Path(destination) == controller.run_dir / "checkpoint.json":
                    raise OSError("injected archive write failure")
                return original_replace(source, destination)

            with patch("controller_safety.os.replace", side_effect=fail_archive_replace):
                with self.assertRaisesRegex(OSError, "injected archive write failure"):
                    controller.run()
            self.assertEqual(controller.checkpoints.load()["phase"], "publishing")
            archived_store = CheckpointStore(controller.run_dir / "checkpoint.json")
            self.assertEqual(archived_store.load()["phase"], "publishing")
            resumed = self.new_controller(resume_id=controller.run_id)
            resumed.run()
        self.assertEqual(archived_store.load()["phase"], "terminal")
        self.assertEqual(self.counter.read_text(), "4")

    def test_resumed_publication_rechecks_legacy_evidence(self):
        self.configure(("mode = human", "mode = none"))
        (self.project / "acceptance.txt").write_text("support", encoding="utf-8")
        environment = self.scripted_environment([
            {}, {}, {},
            {"overrides": {"evidence": [self.evidence("acceptance.txt")]}},
        ])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            with patch.object(controller, "publish_run", side_effect=RuntimeError("publication stopped")):
                with self.assertRaisesRegex(RuntimeError, "publication stopped"):
                    controller.run()
            saved = controller.checkpoints.load()
            saved["active"]["evidence"] = [self.evidence()]
            controller.checkpoints.save(saved)
            resumed = self.new_controller(resume_id=controller.run_id)
            with self.assertRaisesRegex(RuntimeError, "omits acceptance evidence"):
                resumed.run()
        self.assertFalse(controller.run_dir.exists())

    def test_publication_failure_at_status_is_resumable(self):
        self.configure(("mode = human", "mode = none"))
        environment = self.scripted_environment([])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            original_status = controller.write_status

            def fail_complete_status(**values):
                if values.get("stage") == "complete":
                    raise RuntimeError("injected status failure")
                return original_status(**values)

            with patch.object(controller, "write_status", side_effect=fail_complete_status):
                with self.assertRaisesRegex(RuntimeError, "injected status failure"):
                    controller.run()
            self.assertEqual(controller.checkpoints.load()["phase"], "publishing")
            archived_before = (controller.run_dir / "checkpoint.json").read_bytes()
            resumed = self.new_controller(resume_id=controller.run_id)
            resumed.run()
        self.assertEqual((controller.run_dir / "checkpoint.json").read_bytes(), archived_before)
        self.assertEqual(self.counter.read_text(), "4")
        self.assertFalse(controller.private_dir.exists())

    def test_terminal_checkpoint_can_finish_publication(self):
        self.configure(("mode = human", "mode = none"))
        environment = self.scripted_environment([])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            original_save = controller.save_checkpoint

            def fail_after_terminal(**updates):
                original_save(**updates)
                if updates.get("phase") == "terminal":
                    raise RuntimeError("injected terminal failure")

            with patch.object(controller, "save_checkpoint", side_effect=fail_after_terminal):
                with self.assertRaisesRegex(RuntimeError, "injected terminal failure"):
                    controller.run()
            self.assertEqual(controller.checkpoints.load()["phase"], "terminal")
            # Reproduce the archive state left by the old terminal-before-copy order.
            archived_store = CheckpointStore(controller.run_dir / "checkpoint.json")
            archived = archived_store.load()
            archived["phase"] = "publishing"
            archived_store.save(archived)
            resumed = self.new_controller(resume_id=controller.run_id)
            resumed.run()
        self.assertEqual(archived_store.load()["phase"], "terminal")
        self.assertEqual(self.counter.read_text(), "4")
        self.assertFalse(controller.private_dir.exists())

    def test_failed_checkpoint_write_does_not_advance_memory_state(self):
        with patch.dict(os.environ, self.scripted_environment([]), clear=True):
            controller = self.new_controller()
            previous = dict(controller.state)
            with patch.object(controller.checkpoints, "save", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    controller.save_checkpoint(phase="terminal")
        self.assertEqual(controller.state, previous)

    def test_stale_resumer_cannot_overwrite_newer_checkpoint(self):
        with patch.dict(os.environ, self.scripted_environment([]), clear=True):
            controller = self.new_controller()
            controller.run()
            stale = self.new_controller(resume_id=controller.run_id)
            fresh = self.new_controller(resume_id=controller.run_id)
            fresh._restore_agents()
            fresh.save_checkpoint(last_error="newer controller state")
            before = controller.checkpoints.path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "Checkpoint changed"):
                stale.run()
            self.assertEqual(controller.checkpoints.path.read_bytes(), before)
            fresh.adjudicate(
                self._write_review_file(self.review(["evidence.txt"])), reviewer="test"
            )

    def _write_review_file(self, review):
        path = self.root / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def test_sigterm_preserves_live_events_and_thread(self):
        self.configure(("mode = human", "mode = none"))
        environment = self.scripted_environment([{"sleep": 30}])
        ready = self.root / "ready"
        environment["FAKE_CODEX_READY"] = str(ready)
        repository = Path(__file__).resolve().parents[1]
        for filename in ("debate.py", "controller_safety.py", "runtime_isolation.py"):
            shutil.copy2(repository / filename, self.harness / filename)
        proc = subprocess.Popen(
            [sys.executable, str(self.harness / "debate.py"), "--config", str(self.config)],
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "fake Codex did not start")
            raw_logs = list(self.settings.state_dir.glob("*/raw-jsonl/*.jsonl"))
            self.assertEqual(len(raw_logs), 1)
            self.assertIn("partial progress", raw_logs[0].read_text())
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 143, stdout + stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
            if ready.exists():
                try:
                    os.killpg(int(ready.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        checkpoint_path = next(self.settings.state_dir.glob("*/checkpoint.json"))
        saved = CheckpointStore(checkpoint_path).load()
        self.assertIsNotNone(saved["inflight"])
        self.assertEqual(saved["threads"]["prometheus"], "fake-thread-1")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "--retry-inflight"):
                self.new_controller(resume_id=saved["run_id"])
            resumed = self.new_controller(resume_id=saved["run_id"], retry_inflight=True)
            resumed.run()
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertIn("resume", calls[1]["args"])
        self.assertIn("fake-thread-1", calls[1]["args"])

    def test_retry_recovers_first_thread_from_log_after_unclean_exit(self):
        self.configure(("mode = human", "mode = none"))
        environment = self.scripted_environment([])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            controller.prepare_private_runtime()
            controller._restore_agents()
            controller.budget.begin_call()
            sequence = controller.next_sequence()
            controller.save_checkpoint(inflight={
                "agent": "prometheus", "stage": "prometheus_opening", "sequence": sequence,
            })
            raw = controller.private_raw / "001_prometheus_prometheus_opening.jsonl"
            raw.write_text(json.dumps({"type": "thread.started", "thread_id": "recovered-thread"}) + "\n")
            resumed = self.new_controller(resume_id=controller.run_id, retry_inflight=True)
            resumed.run()
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertIn("resume", calls[0]["args"])
        self.assertIn("recovered-thread", calls[0]["args"])

    def test_first_call_failure_retains_thread_for_retry(self):
        self.configure(("mode = human", "mode = none"))
        environment = self.scripted_environment([{"exit_code": 23}])
        with patch.dict(os.environ, environment, clear=True):
            controller = self.new_controller()
            with self.assertRaisesRegex(RuntimeError, "exit code 23"):
                controller.run()
            self.assertEqual(controller.checkpoints.load()["threads"]["prometheus"], "fake-thread-1")
            resumed = self.new_controller(resume_id=controller.run_id, retry_inflight=True)
            resumed.run()
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertIn("resume", calls[1]["args"])
        self.assertIn("fake-thread-1", calls[1]["args"])

    def test_timeouts_preserve_raw_events(self):
        self.configure(("mode = human", "mode = none"))
        for wall_budget in (False, True):
            with self.subTest(wall_budget=wall_budget):
                settings = replace(
                    self.settings,
                    turn_timeout_minutes=0 if wall_budget else 0.01,
                    max_wall_minutes=0.01 if wall_budget else 5,
                )
                environment = self.scripted_environment([{"sleep": 30}])
                with patch.dict(os.environ, environment, clear=True):
                    controller = DebateController(settings)
                    self.localize_controller_files(controller)
                    if wall_budget:
                        controller.run()
                        root = controller.run_dir
                        self.assertTrue((root / "BUDGET_EXHAUSTED.md").exists())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "turn timed out"):
                            controller.run()
                        root = controller.private_dir
                raw_log = next((root / "raw-jsonl").glob("*.jsonl"))
                self.assertIn("partial progress", raw_log.read_text())
                saved = CheckpointStore(root / "checkpoint.json").load()
                self.assertEqual(saved["threads"]["prometheus"], "fake-thread-1")


if __name__ == "__main__":
    unittest.main()
