#!/usr/bin/env python3
"""Deterministic Codex CLI stand-in used only by the test suite."""

from __future__ import annotations

import json
import os
import sys
import time
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
    script_path = os.environ.get("FAKE_CODEX_SCRIPT")
    script = json.loads(Path(script_path).read_text()) if script_path else []
    step = script[count - 1] if count <= len(script) else {}
    call_log = os.environ.get("FAKE_CODEX_CALL_LOG")
    if call_log:
        with Path(call_log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"args": args, "prompt": prompt}) + "\n")

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
            ),
            flush=True,
        )
    if step.get("sleep"):
        print(json.dumps({"type": "item.started", "message": "partial progress"}), flush=True)
        ready = os.environ.get("FAKE_CODEX_READY")
        if ready:
            Path(ready).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(step["sleep"])
    print(json.dumps(event), flush=True)
    if step.get("exit_code"):
        return step["exit_code"]

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
    response = step.get("response", response_for(prompt))
    response.update(step.get("overrides", {}))
    output_file.write_text(
        json.dumps(response),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
