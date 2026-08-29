#!/usr/bin/env python3
"""Deterministic Codex CLI stand-in used only by the test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def response_for(prompt: str) -> dict:
    evidence = [
        {
            "claim": "The fixture exists.",
            "source": "evidence.txt",
            "source_type": "project_file",
            "status": "verified",
            "notes": "Checked by the controller.",
        }
    ]
    if "You are the opening agent" in prompt:
        decision = "PROPOSE"
        proposal = "Opening candidate"
    elif "reduce anchoring" in prompt:
        decision = "PROPOSE"
        proposal = "Independent analysis"
    elif os.environ.get("FAKE_CODEX_ALWAYS_COUNTER") == "1":
        decision = "COUNTER"
        proposal = "Continued standalone candidate"
    elif "ACCEPTANCE IS FORBIDDEN" in prompt:
        decision = "COUNTER"
        proposal = "Improved standalone candidate"
    else:
        decision = "ACCEPT"
        proposal = "Candidate accepted"
    return {
        "decision": decision,
        "critique": [],
        "proposal": proposal,
        "blocking_issues": [],
        "rationale": "Deterministic fixture response.",
        "evidence": evidence,
    }


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli fake")
        return 0
    if "--help" in args:
        print("--output-schema --output-last-message --json --sandbox resume")
        return 0

    prompt = sys.stdin.read()
    counter_file = Path(os.environ["FAKE_CODEX_COUNTER"])
    count = int(counter_file.read_text(encoding="utf-8") or "0") + 1
    counter_file.write_text(str(count), encoding="utf-8")

    event = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 5,
        },
    }
    if "resume" not in args:
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": f"fake-thread-{count}"}
            )
        )
    print(json.dumps(event))

    fail_marker = os.environ.get("FAKE_CODEX_FAIL_MARKER")
    if (
        fail_marker
        and "ACCEPTANCE IS PERMITTED" in prompt
        and not Path(fail_marker).exists()
    ):
        Path(fail_marker).write_text("failed once", encoding="utf-8")
        return 23

    output_index = args.index("--output-last-message") + 1
    output_file = Path(args[output_index])
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(response_for(prompt)),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
