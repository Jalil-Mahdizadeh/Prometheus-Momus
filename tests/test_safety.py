from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from controller_safety import (
    BudgetExceeded,
    BudgetTracker,
    CheckpointStore,
    EvidenceError,
    audit_evidence,
    parse_codex_usage,
    validate_adjudication,
)


class UsageAndBudgetTests(unittest.TestCase):
    def tracker(self, **overrides):
        values = {
            "max_calls": 2,
            "max_wall_seconds": 60,
            "max_total_tokens": 100,
            "max_estimated_cost_usd": 0,
            "input_usd_per_million": 1,
            "cached_input_usd_per_million": 0.5,
            "output_usd_per_million": 2,
        }
        values.update(overrides)
        return BudgetTracker(**values)

    def test_parses_terminal_usage_and_enforces_call_limit(self):
        usage = parse_codex_usage(
            json.dumps({"type": "item.started", "usage": {"input_tokens": 999}})
            + "\n"
            + json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 3,
                        "output_tokens": 4,
                    },
                }
            )
        )
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 14)
        tracker = self.tracker()
        tracker.begin_call()
        tracker.record_usage(usage)
        tracker.begin_call()
        with self.assertRaises(BudgetExceeded):
            tracker.begin_call()

    def test_missing_usage_fails_closed(self):
        tracker = self.tracker()
        tracker.begin_call()
        with self.assertRaisesRegex(BudgetExceeded, "no parseable token usage"):
            tracker.record_usage(None)

    def test_zero_cached_rate_uses_conservative_input_rate(self):
        tracker = self.tracker(
            max_estimated_cost_usd=0.000005,
            cached_input_usd_per_million=0,
        )
        tracker.begin_call()
        with self.assertRaisesRegex(BudgetExceeded, "Estimated-cost budget"):
            tracker.record_usage(
                parse_codex_usage(
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 10,
                                "output_tokens": 0,
                            },
                        }
                    )
                )
            )


class EvidenceAndCheckpointTests(unittest.TestCase):
    def test_project_file_is_hashed_and_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fact.txt").write_text("fact", encoding="utf-8")
            records = audit_evidence(
                [
                    {
                        "claim": "A fact",
                        "source": "fact.txt#L1",
                        "source_type": "project_file",
                        "status": "verified",
                        "notes": "",
                    }
                ],
                project_root=root,
                agent="test",
                stage="test",
            )
            self.assertTrue(records[0]["mechanically_verified"])
            self.assertEqual(len(records[0]["sha256"]), 64)
            isolated = audit_evidence(
                [
                    {
                        "claim": "Same fact",
                        "source": "/workspace/fact.txt#L1",
                        "source_type": "project_file",
                        "status": "verified",
                        "notes": "",
                    }
                ],
                project_root=root,
                agent="test",
                stage="test",
            )
            self.assertEqual(isolated[0]["sha256"], records[0]["sha256"])
            with self.assertRaises(EvidenceError):
                audit_evidence(
                    [
                        {
                            "claim": "escape",
                            "source": "../outside",
                            "source_type": "project_file",
                            "status": "verified",
                            "notes": "",
                        }
                    ],
                    project_root=root,
                    agent="test",
                    stage="test",
                )

    def test_approval_requires_independent_external_checks(self):
        review = {
            "decision": "APPROVE",
            "rationale": "Verified independently.",
            "blocking_issues": [],
            "evidence_checks": [],
        }
        with self.assertRaisesRegex(ValueError, "requires independent verification"):
            validate_adjudication(review, required_sources={"https://example.test"})
        review["evidence_checks"] = [
            {
                "source": "https://example.test",
                "result": "verified",
                "notes": "Checked.",
            }
        ]
        self.assertEqual(
            validate_adjudication(
                review, required_sources={"https://example.test"}
            )["decision"],
            "APPROVE",
        )

    def test_checkpoint_is_versioned_and_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            store = CheckpointStore(path)
            store.save({"phase": "debating"})
            first = store.load()
            self.assertEqual(first["phase"], "debating")
            self.assertEqual(first["checkpoint_version"], 1)
            store.save(first | {"phase": "terminal"})
            self.assertEqual(store.load()["phase"], "terminal")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
