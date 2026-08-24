#!/usr/bin/env python3
"""
Prometheus-Momus
================

A domain-neutral, autonomous two-Codex adversarial debate harness.

Prometheus builds/synthesizes.
Momus independently analyzes, challenges, and counterproposes.
The controller alternates persistent Codex threads until a guarded
acceptance or a configured round limit is reached.

The harness deliberately keeps live debate content outside the project
workspace until the run is finished. This makes the second agent's initial
analysis operationally blind to the first agent's response and prevents
later agents from casually reading the full debate transcript instead of
the explicitly supplied current candidate.

Python standard library only. Designed primarily for Linux/macOS because
run locking uses fcntl.
"""

from __future__ import annotations

import argparse
import configparser
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


VERSION = "1.0.0"
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "config.ini"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def die(message: str, code: int = 1) -> "None":
    print(f"\nERROR: {message}\n", file=sys.stderr)
    raise SystemExit(code)


def as_bool(raw: str, *, key: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true/false, got {raw!r}")


def resolve_from(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_id(text: str) -> str:
    return "P-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def slugify(text: str, limit: int = 80) -> str:
    value = re_sub_non_alnum(text.lower())
    while "__" in value:
        value = value.replace("__", "_")
    return (value.strip("_") or "turn")[:limit]


def re_sub_non_alnum(value: str) -> str:
    # Kept separate to avoid importing re for a single operation.
    return "".join(ch if ch.isalnum() else "_" for ch in value)


def get_thread_id(jsonl_text: str) -> str:
    for line in jsonl_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if not thread_id and isinstance(event.get("thread"), dict):
                thread_id = event["thread"].get("id")
            if thread_id:
                return str(thread_id)

    raise RuntimeError("Could not find thread.started/thread_id in Codex JSONL output.")


def validate_response(response: object, agent_name: str) -> dict:
    required = {
        "decision",
        "critique",
        "proposal",
        "blocking_issues",
        "rationale",
    }

    if not isinstance(response, dict):
        raise RuntimeError(f"{agent_name} response is not a JSON object.")

    missing = required - set(response)
    if missing:
        raise RuntimeError(
            f"{agent_name} response is missing required fields: {sorted(missing)}"
        )

    if response["decision"] not in {"PROPOSE", "COUNTER", "ACCEPT"}:
        raise RuntimeError(
            f"{agent_name} returned invalid decision: {response['decision']!r}"
        )

    if not isinstance(response["critique"], list):
        raise RuntimeError(f"{agent_name}.critique must be an array/list.")
    if not isinstance(response["blocking_issues"], list):
        raise RuntimeError(f"{agent_name}.blocking_issues must be an array/list.")
    if not isinstance(response["proposal"], str):
        raise RuntimeError(f"{agent_name}.proposal must be a string.")
    if not isinstance(response["rationale"], str):
        raise RuntimeError(f"{agent_name}.rationale must be a string.")

    return response


def pretty_response(response: dict) -> str:
    chunks = [f"**Decision:** {response['decision']}"]

    if response["critique"]:
        chunks.append("\n## Critique\n")
        chunks.extend(f"- {item}" for item in response["critique"])

    if response["blocking_issues"]:
        chunks.append("\n## Blocking issues\n")
        chunks.extend(f"- {item}" for item in response["blocking_issues"])

    chunks.append("\n## Proposal / current state\n")
    chunks.append(response["proposal"])
    chunks.append("\n## Rationale\n")
    chunks.append(response["rationale"])

    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    config_path: Path
    project_root: Path
    task_file: Path
    prometheus_file: Path
    momus_file: Path
    schema_file: Path
    runs_dir: Path

    min_counter_rounds: int
    max_counter_rounds: int
    blind_second_agent: bool
    final_acceptance_audit: bool
    max_protocol_repairs: int

    model: str
    reasoning_effort: str
    web_search: str
    sandbox: str
    skip_git_repo_check: bool
    ignore_user_config: bool
    ignore_rules: bool

    heartbeat_seconds: int
    turn_timeout_minutes: int

    publish_prompts: bool
    publish_raw_jsonl: bool
    keep_private_runtime_on_success: bool
    keep_private_runtime_on_failure: bool


def load_settings(config_path: Path, project_root_override: Optional[str]) -> Settings:
    if not config_path.exists():
        die(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")

    required_sections = {"debate", "codex", "paths", "output"}
    missing_sections = required_sections - set(parser.sections())
    if missing_sections:
        die(f"Config is missing sections: {sorted(missing_sections)}")

    package_dir = config_path.resolve().parent

    configured_root = parser["paths"].get("project_root", "..").strip() or ".."
    project_root = (
        Path(project_root_override).expanduser().resolve()
        if project_root_override
        else resolve_from(package_dir, configured_root)
    )

    task_file = resolve_from(package_dir, parser["paths"].get("task_file", "task.md"))
    prometheus_file = resolve_from(
        package_dir, parser["paths"].get("prometheus_file", "Prometheus.md")
    )
    momus_file = resolve_from(package_dir, parser["paths"].get("momus_file", "Momus.MD"))
    schema_file = resolve_from(package_dir, parser["paths"].get("schema_file", "schema.json"))
    runs_dir = resolve_from(package_dir, parser["paths"].get("runs_dir", "runs"))

    try:
        min_rounds = int(parser["debate"].get("min_counter_rounds", "3"))
        max_rounds = int(parser["debate"].get("max_counter_rounds", "8"))
        max_repairs = int(parser["debate"].get("max_protocol_repairs", "2"))
        heartbeat = int(parser["debate"].get("heartbeat_seconds", "60"))
        timeout = int(parser["debate"].get("turn_timeout_minutes", "0"))

        blind = as_bool(
            parser["debate"].get("blind_second_agent", "true"),
            key="blind_second_agent",
        )
        final_audit = as_bool(
            parser["debate"].get("final_acceptance_audit", "true"),
            key="final_acceptance_audit",
        )

        skip_git = as_bool(
            parser["codex"].get("skip_git_repo_check", "true"),
            key="skip_git_repo_check",
        )
        ignore_user_config = as_bool(
            parser["codex"].get("ignore_user_config", "false"),
            key="ignore_user_config",
        )
        ignore_rules = as_bool(
            parser["codex"].get("ignore_rules", "false"),
            key="ignore_rules",
        )

        publish_prompts = as_bool(
            parser["output"].get("publish_prompts", "true"),
            key="publish_prompts",
        )
        publish_raw = as_bool(
            parser["output"].get("publish_raw_jsonl", "true"),
            key="publish_raw_jsonl",
        )
        keep_success = as_bool(
            parser["output"].get("keep_private_runtime_on_success", "false"),
            key="keep_private_runtime_on_success",
        )
        keep_failure = as_bool(
            parser["output"].get("keep_private_runtime_on_failure", "true"),
            key="keep_private_runtime_on_failure",
        )

    except ValueError as exc:
        die(f"Invalid configuration value: {exc}")

    if min_rounds < 0:
        die("min_counter_rounds must be >= 0")
    if max_rounds < 1:
        die("max_counter_rounds must be >= 1")
    if min_rounds > max_rounds:
        die("min_counter_rounds cannot exceed max_counter_rounds")
    if max_repairs < 0:
        die("max_protocol_repairs must be >= 0")
    if heartbeat < 1:
        die("heartbeat_seconds must be >= 1")
    if timeout < 0:
        die("turn_timeout_minutes must be >= 0")

    sandbox = parser["codex"].get("sandbox", "read-only").strip() or "read-only"
    allowed_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
    if sandbox not in allowed_sandboxes:
        die(
            f"sandbox must be one of {sorted(allowed_sandboxes)}, got {sandbox!r}"
        )

    web_search = parser["codex"].get("web_search", "inherit").strip().lower() or "inherit"
    # Codex CLI has evolved over time; keep validation conservative but allow
    # the currently used modes and a no-override setting.
    if web_search not in {"inherit", "disabled", "cached", "indexed", "live"}:
        die(
            "web_search must be inherit, disabled, cached, indexed, or live; "
            f"got {web_search!r}"
        )

    if not project_root.exists() or not project_root.is_dir():
        die(f"Project root does not exist or is not a directory: {project_root}")

    return Settings(
        config_path=config_path.resolve(),
        project_root=project_root,
        task_file=task_file,
        prometheus_file=prometheus_file,
        momus_file=momus_file,
        schema_file=schema_file,
        runs_dir=runs_dir,

        min_counter_rounds=min_rounds,
        max_counter_rounds=max_rounds,
        blind_second_agent=blind,
        final_acceptance_audit=final_audit,
        max_protocol_repairs=max_repairs,

        model=parser["codex"].get("model", "").strip(),
        reasoning_effort=parser["codex"].get("reasoning_effort", "").strip(),
        web_search=web_search,
        sandbox=sandbox,
        skip_git_repo_check=skip_git,
        ignore_user_config=ignore_user_config,
        ignore_rules=ignore_rules,

        heartbeat_seconds=heartbeat,
        turn_timeout_minutes=timeout,

        publish_prompts=publish_prompts,
        publish_raw_jsonl=publish_raw,
        keep_private_runtime_on_success=keep_success,
        keep_private_runtime_on_failure=keep_failure,
    )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class DebateController:
    def __init__(self, settings: Settings):
        self.s = settings
        self.run_id = (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )

        tmp_root = Path(os.environ.get("TMPDIR", "/tmp")).expanduser().resolve()
        self.private_dir = tmp_root / f"prometheus-momus-{self.run_id}"
        self.private_prompts = self.private_dir / "prompts"
        self.private_raw = self.private_dir / "raw-jsonl"
        self.private_responses = self.private_dir / "responses"
        self.private_transcript = self.private_dir / "DEBATE_TRANSCRIPT.md"
        self.private_history = self.private_dir / "history.jsonl"
        self.private_blind = self.private_dir / "momus_blind_analysis.json"

        self.run_dir = self.s.runs_dir / self.run_id
        self.status_file = PACKAGE_DIR / "RUN_STATUS.json"
        self.latest_file = PACKAGE_DIR / "LATEST_RUN.txt"
        self.lock_path = PACKAGE_DIR / ".debate.lock"

        self._sequence = 0
        self._lock_handle = None
        self._success = False

        self.task_text = self._read_required(self.s.task_file, "task")
        self.prometheus_role = self._read_required(
            self.s.prometheus_file, "Prometheus role"
        )
        self.momus_role = self._read_required(self.s.momus_file, "Momus role")
        self.schema = self._load_schema()

        self.prometheus: Optional[Agent] = None
        self.momus: Optional[Agent] = None

    @staticmethod
    def _read_required(path: Path, label: str) -> str:
        if not path.exists():
            die(f"Missing {label} file: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            die(f"{label.capitalize()} file is empty: {path}")
        return text

    def _load_schema(self) -> dict:
        if not self.s.schema_file.exists():
            die(f"Missing schema file: {self.s.schema_file}")
        try:
            schema = json.loads(self.s.schema_file.read_text(encoding="utf-8"))
        except Exception as exc:
            die(f"Invalid schema JSON: {exc}")
        return schema

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def acquire_lock(self) -> None:
        self._lock_handle = self.lock_path.open("w")
        try:
            fcntl.flock(
                self._lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            self._lock_handle.close()
            die(
                "Another debate.py process appears to be running from this "
                "package. Concurrent runs are intentionally blocked."
            )

        self._lock_handle.write(str(os.getpid()))
        self._lock_handle.flush()

    def release_lock(self) -> None:
        if not self._lock_handle:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def prepare_private_runtime(self) -> None:
        self.private_prompts.mkdir(parents=True, exist_ok=True)
        self.private_raw.mkdir(parents=True, exist_ok=True)
        self.private_responses.mkdir(parents=True, exist_ok=True)

        self.private_transcript.write_text(
            "\n".join(
                [
                    "# Prometheus–Momus Autonomous Debate Transcript",
                    "",
                    f"- Run ID: `{self.run_id}`",
                    f"- Started: `{now_iso()}`",
                    f"- Project root: `{self.s.project_root}`",
                    f"- Model override: `{self.s.model or 'inherit'}`",
                    f"- Reasoning effort override: `{self.s.reasoning_effort or 'inherit'}`",
                    f"- Web search: `{self.s.web_search}`",
                    f"- Sandbox: `{self.s.sandbox}`",
                    f"- Minimum counter-rounds: `{self.s.min_counter_rounds}`",
                    f"- Maximum counter-rounds: `{self.s.max_counter_rounds}`",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        # Snapshot semantic inputs and execution configuration before the run.
        shutil.copy2(self.s.task_file, self.private_dir / "task.snapshot.md")
        shutil.copy2(self.s.prometheus_file, self.private_dir / "Prometheus.snapshot.md")
        shutil.copy2(self.s.momus_file, self.private_dir / "Momus.snapshot.MD")
        shutil.copy2(self.s.schema_file, self.private_dir / "schema.snapshot.json")
        shutil.copy2(self.s.config_path, self.private_dir / "config.snapshot.ini")

    def write_status(self, **extra: object) -> None:
        payload = {
            "run_id": self.run_id,
            "updated_at": now_iso(),
            "pid": os.getpid(),
            "project_root": str(self.s.project_root),
            "model": self.s.model or "inherit",
            "reasoning_effort": self.s.reasoning_effort or "inherit",
            "web_search": self.s.web_search,
            "sandbox": self.s.sandbox,
            "private_transcript": str(self.private_transcript),
            **extra,
        }
        self.status_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def append_transcript(self, title: str, response: dict) -> None:
        with self.private_transcript.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n# {title}\n\n")
            handle.write(pretty_response(response))
            handle.write("\n")

    def append_history(
        self,
        stage: str,
        agent: "Agent",
        response: dict,
        proposal_id_value: Optional[str],
    ) -> None:
        record = {
            "timestamp": now_iso(),
            "stage": stage,
            "agent": agent.display_name,
            "thread_id": agent.thread_id,
            "decision": response["decision"],
            "candidate_id": proposal_id_value,
            "critique": response["critique"],
            "proposal": response["proposal"],
            "blocking_issues": response["blocking_issues"],
            "rationale": response["rationale"],
        }
        with self.private_history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def make_candidate(agent: "Agent", response: dict) -> dict:
        return {
            "candidate_id": candidate_id(response["proposal"]),
            "author": agent.display_name,
            "proposal": response["proposal"],
            "critique": response["critique"],
            "blocking_issues": response["blocking_issues"],
            "rationale": response["rationale"],
        }

    # ------------------------------------------------------------------
    # Prompt protocol
    # ------------------------------------------------------------------

    @property
    def common_protocol(self) -> str:
        return """
AUTONOMOUS DEBATE PROTOCOL

The task below is authoritative.

The JSON field named `proposal` means the COMPLETE CURRENT CANDIDATE,
RESEARCH STATE, PLAN, DESIGN, ANSWER, OR RECOMMENDATION appropriate to the
task. It does not require a particular artifact type.

Rules:

1. Inspect the project when useful and allowed by the task.
2. Do not invent project assets or evidence that do not exist.
3. Distinguish material objections from cosmetic preferences.
4. Preserve strong elements of the opponent's work when counterproposing.
5. A COUNTER must provide a complete standalone replacement proposal/state,
   not merely a list of edits.
6. Do not compromise merely to end the debate.
7. Do not repeat objections that have already been convincingly resolved.
8. ACCEPT means that, after actively attempting another falsification, you
   cannot justify a material improvement under the task's criteria.
9. Blocking issues and ACCEPT are logically incompatible.
10. Evidence, external research, code inspection, calculations, or other
    tools should be used when the task calls for them.
11. The debate may legitimately end in a negative recommendation if that is
    the strongest conclusion.
"""

    def opening_prompt(self) -> str:
        return f"""
{self.prometheus_role}

{self.common_protocol}

TASK
====

{self.task_text}

You are the opening agent.

Develop an independent, rigorous first position. Do the analysis required by
the task before converging on a recommendation.

Return decision="PROPOSE".

The proposal field must contain a complete standalone current state that the
other agent can meaningfully challenge.
"""

    def blind_prompt(self, opening_candidate: Optional[dict]) -> str:
        blindness = """
You have NOT been shown Prometheus's opening response.

Do not inspect Codex session-rollout files, shell history, process metadata,
temporary debate-runtime directories, or other orchestration artifacts in an
attempt to discover Prometheus's hidden work.

Perform your own independent analysis from the project, the task, your role,
and legitimate external sources.
"""
        if not self.s.blind_second_agent and opening_candidate is not None:
            blindness = f"""
For this run, blind pre-analysis is disabled. Prometheus's opening state is
shown below for context, but still perform an independent analysis before
judging it:

{json.dumps(opening_candidate, indent=2, ensure_ascii=False)}
"""

        return f"""
{self.momus_role}

{self.common_protocol}

TASK
====

{self.task_text}

{blindness}

Return decision="PROPOSE".

The proposal field must contain your complete independent current state. This
stage exists to reduce anchoring before the adversarial exchange begins.
"""

    def challenge_prompt(self, role_text: str, active: dict, completed_rounds: int) -> str:
        acceptance_allowed = completed_rounds >= self.s.min_counter_rounds

        if acceptance_allowed:
            acceptance = """
ACCEPTANCE IS PERMITTED.

Before accepting, actively try once more to falsify the current candidate:
look for a stronger alternative, hidden assumption, correctness failure,
evidence gap, simpler solution, feasibility problem, or conflict with the
task.

ACCEPT only if no MATERIAL improvement is justified.
If ACCEPT, blocking_issues must be empty.
"""
        else:
            acceptance = """
ACCEPTANCE IS FORBIDDEN AT THIS STAGE.

Return COUNTER. Find the strongest material improvement you can justify.
Do not invent cosmetic criticism merely because acceptance is forbidden.
"""

        return f"""
{role_text}

{self.common_protocol}

TASK
====

{self.task_text}

OPPONENT'S CURRENT CANDIDATE
============================

{json.dumps(active, indent=2, ensure_ascii=False)}

This is adversarial counter-round {completed_rounds + 1}.

Critically evaluate the current candidate against the task. Use your own
persistent reasoning history and, where useful, additional inspection or
research.

{acceptance}

If COUNTER:
- explain the strongest material deficiencies;
- resolve them rather than merely criticizing;
- place the COMPLETE replacement proposal/state in `proposal`.
"""

    def forced_counter_prompt(self, active: dict) -> str:
        return f"""
Your previous response attempted ACCEPT before the configured minimum number
of adversarial counter-rounds had completed.

Acceptance is not permitted yet.

Current candidate:

{json.dumps(active, indent=2, ensure_ascii=False)}

Find the strongest remaining MATERIAL improvement. Do not manufacture
cosmetic objections. Return decision="COUNTER" with a complete standalone
replacement proposal/state.
"""

    def repair_accept_prompt(self, active: dict) -> str:
        return f"""
Your previous response returned ACCEPT while also listing blocking issues.
Those outputs are inconsistent.

Current candidate:

{json.dumps(active, indent=2, ensure_ascii=False)}

Re-evaluate it.

Either:
1. return ACCEPT with blocking_issues=[] if no material issue remains; or
2. return COUNTER with a complete replacement proposal/state that addresses
   the material issue.

Do not lower the acceptance standard.
"""

    def final_acceptance_prompt(self, role_text: str, active: dict, rounds: int) -> str:
        return f"""
{role_text}

{self.common_protocol}

TASK
====

{self.task_text}

TENTATIVELY ACCEPTED CANDIDATE
===============================

{json.dumps(active, indent=2, ensure_ascii=False)}

You tentatively accepted this candidate after {rounds} completed
counter-rounds.

This is a mandatory FINAL ACCEPTANCE AUDIT.

Make one final serious attempt to defeat the candidate. Recheck the task's
success criteria, evidence, assumptions, feasibility, simpler alternatives,
and any domain-specific failure modes.

If a material issue is found, return COUNTER with a complete corrected
replacement state.

Return ACCEPT only if the candidate still survives this final attack.
If ACCEPT, blocking_issues must be empty.
"""

    # ------------------------------------------------------------------
    # Protocol repair
    # ------------------------------------------------------------------

    def enforce_allowed_decision(
        self,
        agent: "Agent",
        response: dict,
        allowed: set[str],
        stage_label: str,
        active: Optional[dict] = None,
    ) -> dict:
        repairs = 0

        while response["decision"] not in allowed:
            if repairs >= self.s.max_protocol_repairs:
                raise RuntimeError(
                    f"{agent.display_name} repeatedly returned a decision "
                    f"invalid for {stage_label}: {response['decision']!r}. "
                    f"Allowed: {sorted(allowed)}"
                )

            repairs += 1
            current_text = (
                json.dumps(active, indent=2, ensure_ascii=False)
                if active is not None
                else "(no opponent candidate is active in this stage)"
            )

            repair_prompt = f"""
Your previous structured response used decision={response['decision']!r},
which is not valid for the current protocol stage: {stage_label}.

Allowed decisions for this stage are:

{sorted(allowed)}

Current candidate/context:

{current_text}

Keep the substantive analysis if it remains valid, but return a fresh complete
structured response using one of the allowed decisions. Do not change the
scientific/technical conclusion merely to satisfy the protocol label.
"""

            response = agent.run(
                repair_prompt,
                stage=f"{slugify(stage_label)}_decision_repair_{repairs}",
            )

        return response

    def enforce_premature_acceptance(
        self, agent: "Agent", response: dict, active: dict, stage_prefix: str
    ) -> dict:
        repairs = 0
        while response["decision"] == "ACCEPT":
            if repairs >= self.s.max_protocol_repairs:
                raise RuntimeError(
                    f"{agent.display_name} repeatedly attempted premature ACCEPT."
                )
            repairs += 1
            response = agent.run(
                self.forced_counter_prompt(active),
                stage=f"{stage_prefix}_premature_accept_repair_{repairs}",
            )
        return response

    def enforce_accept_consistency(
        self, agent: "Agent", response: dict, active: dict, stage_prefix: str
    ) -> dict:
        repairs = 0
        while response["decision"] == "ACCEPT" and response["blocking_issues"]:
            if repairs >= self.s.max_protocol_repairs:
                raise RuntimeError(
                    f"{agent.display_name} repeatedly returned ACCEPT with blocking issues."
                )
            repairs += 1
            response = agent.run(
                self.repair_accept_prompt(active),
                stage=f"{stage_prefix}_accept_consistency_repair_{repairs}",
            )
        return response

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------

    def write_manifest(self, outcome: str, active: Optional[dict]) -> None:
        manifest = {
            "package": "Prometheus-Momus",
            "version": VERSION,
            "run_id": self.run_id,
            "outcome": outcome,
            "started_or_updated": now_iso(),
            "project_root": str(self.s.project_root),
            "task_sha256": sha256_file(self.s.task_file),
            "prometheus_role_sha256": sha256_file(self.s.prometheus_file),
            "momus_role_sha256": sha256_file(self.s.momus_file),
            "schema_sha256": sha256_file(self.s.schema_file),
            "config_sha256": sha256_file(self.s.config_path),
            "settings": {
                **{
                    key: value
                    for key, value in asdict(self.s).items()
                    if key
                    not in {
                        "config_path",
                        "project_root",
                        "task_file",
                        "prometheus_file",
                        "momus_file",
                        "schema_file",
                        "runs_dir",
                    }
                },
                "model": self.s.model or "inherit",
                "reasoning_effort": self.s.reasoning_effort or "inherit",
            },
            "prometheus_thread": self.prometheus.thread_id if self.prometheus else None,
            "momus_thread": self.momus.thread_id if self.momus else None,
            "final_candidate_id": active.get("candidate_id") if active else None,
            "final_candidate_author": active.get("author") if active else None,
        }
        (self.private_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def publish_run(self) -> None:
        self.s.runs_dir.mkdir(parents=True, exist_ok=True)
        if self.run_dir.exists():
            raise RuntimeError(f"Run directory already exists: {self.run_dir}")

        self.run_dir.mkdir(parents=True)

        # Core artifacts.
        for path in self.private_dir.iterdir():
            if path.is_dir():
                continue
            shutil.copy2(path, self.run_dir / path.name)

        if self.s.publish_prompts and self.private_prompts.exists():
            shutil.copytree(self.private_prompts, self.run_dir / "prompts")

        if self.s.publish_raw_jsonl and self.private_raw.exists():
            shutil.copytree(self.private_raw, self.run_dir / "raw-jsonl")

        if self.private_responses.exists():
            shutil.copytree(self.private_responses, self.run_dir / "responses")

        self.latest_file.write_text(
            str(self.run_dir.resolve()) + "\n",
            encoding="utf-8",
        )

    def write_consensus(
        self,
        active: dict,
        accepting_agent: "Agent",
        acceptance_response: dict,
        rounds: int,
    ) -> None:
        caveats = acceptance_response["critique"]
        caveat_text = (
            "\n".join(f"- {item}" for item in caveats)
            if caveats
            else "None reported."
        )

        text = f"""# Prometheus–Momus Debate — Consensus

## Status

**CONSENSUS REACHED**

- Run ID: `{self.run_id}`
- Final candidate: `{active['candidate_id']}`
- Candidate author: `{active['author']}`
- Accepted by: `{accepting_agent.display_name}`
- Completed adversarial counter-rounds: `{rounds}`
- Prometheus thread: `{self.prometheus.thread_id}`
- Momus thread: `{self.momus.thread_id}`
- Model override: `{self.s.model or 'inherit'}`
- Reasoning effort override: `{self.s.reasoning_effort or 'inherit'}`
- Web search: `{self.s.web_search}`
- Sandbox: `{self.s.sandbox}`

## Final Proposal / State

{active['proposal']}

## Acceptance Rationale

{acceptance_response['rationale']}

## Remaining Non-blocking Caveats

{caveat_text}

## Interpretation

Consensus means that the two persistent agents could no longer justify a
material improvement under the configured protocol.

Consensus is not empirical validation, legal advice, proof of correctness,
or proof that external research was exhaustive.
"""
        (self.private_dir / "CONSENSUS.md").write_text(text, encoding="utf-8")
        (self.private_dir / "final_candidate.json").write_text(
            json.dumps(active, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_no_consensus(self, active: dict, rounds: int) -> None:
        issues = active["blocking_issues"]
        issue_text = (
            "\n".join(f"- {item}" for item in issues)
            if issues
            else "No blocking issues were explicitly listed in the latest state."
        )

        text = f"""# Prometheus–Momus Debate — No Consensus

## Status

**NO CONSENSUS WITHIN THE CONFIGURED ROUND LIMIT**

- Run ID: `{self.run_id}`
- Maximum counter-rounds: `{self.s.max_counter_rounds}`
- Completed counter-rounds: `{rounds}`
- Latest candidate: `{active['candidate_id']}`
- Latest candidate author: `{active['author']}`
- Prometheus thread: `{self.prometheus.thread_id}`
- Momus thread: `{self.momus.thread_id}`

## Latest Proposal / State

{active['proposal']}

## Remaining Blocking Issues

{issue_text}

## Interpretation

The round limit was reached. Do not treat the latest state as consensus.
"""
        (self.private_dir / "NO_CONSENSUS.md").write_text(text, encoding="utf-8")
        (self.private_dir / "final_candidate.json").write_text(
            json.dumps(active, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.acquire_lock()
        active: Optional[dict] = None
        outcome = "failed"

        try:
            self.prepare_private_runtime()

            self.prometheus = Agent(
                controller=self,
                machine_name="prometheus",
                display_name="Prometheus",
            )
            self.momus = Agent(
                controller=self,
                machine_name="momus",
                display_name="Momus",
            )

            print("\n" + "=" * 72)
            print(" PROMETHEUS–MOMUS AUTONOMOUS TWO-CODEX DEBATE")
            print("=" * 72)
            print(f"Run ID:                  {self.run_id}")
            print(f"Project root:            {self.s.project_root}")
            print(f"Model override:          {self.s.model or 'inherit'}")
            print(f"Reasoning effort:        {self.s.reasoning_effort or 'inherit'}")
            print(f"Web search:              {self.s.web_search}")
            print(f"Sandbox:                 {self.s.sandbox}")
            print(f"Minimum counter-rounds:  {self.s.min_counter_rounds}")
            print(f"Maximum counter-rounds:  {self.s.max_counter_rounds}")
            print(f"Private live transcript: {self.private_transcript}")
            print("\nMonitor in another shell with:")
            print(f"  tail -f '{self.private_transcript}'\n")

            self.write_status(state="starting", stage="initialization")

            # Prometheus opening.
            r1 = self.prometheus.run(self.opening_prompt(), "prometheus_opening")
            r1 = self.enforce_allowed_decision(
                self.prometheus,
                r1,
                {"PROPOSE"},
                "Prometheus opening",
            )

            opening = self.make_candidate(self.prometheus, r1)
            active = opening
            self.append_history("prometheus_opening", self.prometheus, r1, opening["candidate_id"])
            self.append_transcript(
                f"PROMETHEUS OPENING — {opening['candidate_id']}", r1
            )

            # Momus independent pre-analysis.
            blind = self.momus.run(
                self.blind_prompt(opening if not self.s.blind_second_agent else None),
                "momus_blind_analysis",
            )
            blind = self.enforce_allowed_decision(
                self.momus,
                blind,
                {"PROPOSE"},
                "Momus independent pre-analysis",
            )

            self.private_blind.write_text(
                json.dumps(blind, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.append_history("momus_blind_analysis", self.momus, blind, None)
            self.append_transcript("MOMUS BLIND INDEPENDENT ANALYSIS", blind)

            # Alternating debate. Momus challenges Prometheus first.
            current = self.momus
            completed_rounds = 0

            while completed_rounds < self.s.max_counter_rounds:
                role = (
                    self.prometheus_role
                    if current is self.prometheus
                    else self.momus_role
                )

                response = current.run(
                    self.challenge_prompt(role, active, completed_rounds),
                    f"counter_round_{completed_rounds + 1}_challenge",
                )
                response = self.enforce_allowed_decision(
                    current,
                    response,
                    {"COUNTER", "ACCEPT"},
                    f"counter-round {completed_rounds + 1}",
                    active,
                )

                if (
                    response["decision"] == "ACCEPT"
                    and completed_rounds < self.s.min_counter_rounds
                ):
                    response = self.enforce_premature_acceptance(
                        current,
                        response,
                        active,
                        f"counter_round_{completed_rounds + 1}",
                    )
                    response = self.enforce_allowed_decision(
                        current,
                        response,
                        {"COUNTER", "ACCEPT"},
                        f"counter-round {completed_rounds + 1} after premature-accept repair",
                        active,
                    )

                response = self.enforce_accept_consistency(
                    current,
                    response,
                    active,
                    f"counter_round_{completed_rounds + 1}",
                )

                if response["decision"] == "ACCEPT" and self.s.final_acceptance_audit:
                    print(
                        f"\n{current.display_name} tentatively accepted "
                        f"{active['candidate_id']}; running final acceptance audit..."
                    )
                    response = current.run(
                        self.final_acceptance_prompt(role, active, completed_rounds),
                        "final_acceptance_audit",
                    )
                    response = self.enforce_allowed_decision(
                        current,
                        response,
                        {"COUNTER", "ACCEPT"},
                        "final acceptance audit",
                        active,
                    )
                    response = self.enforce_accept_consistency(
                        current,
                        response,
                        active,
                        "final_acceptance_audit",
                    )

                if response["decision"] == "ACCEPT":
                    self.append_history(
                        "acceptance",
                        current,
                        response,
                        active["candidate_id"],
                    )
                    self.append_transcript(
                        f"{current.display_name.upper()} ACCEPTS {active['candidate_id']}",
                        response,
                    )
                    self.write_consensus(
                        active,
                        current,
                        response,
                        completed_rounds,
                    )
                    outcome = "consensus"
                    self.write_manifest(outcome, active)
                    self.publish_run()
                    self.write_status(
                        state="consensus",
                        stage="complete",
                        run_dir=str(self.run_dir),
                        final_candidate=active["candidate_id"],
                        accepted_by=current.display_name,
                    )
                    self._success = True

                    print("\n" + "=" * 72)
                    print(" CONSENSUS REACHED")
                    print("=" * 72)
                    print(f"Final candidate: {active['candidate_id']}")
                    print(f"Proposed by:     {active['author']}")
                    print(f"Accepted by:     {current.display_name}")
                    print(f"Run archive:     {self.run_dir}")
                    print(f"Consensus:       {self.run_dir / 'CONSENSUS.md'}")
                    return

                if response["decision"] != "COUNTER":
                    raise RuntimeError(
                        f"Unexpected decision during debate: {response['decision']}"
                    )

                completed_rounds += 1
                active = self.make_candidate(current, response)

                self.append_history(
                    f"counter_round_{completed_rounds}",
                    current,
                    response,
                    active["candidate_id"],
                )
                self.append_transcript(
                    f"COUNTER ROUND {completed_rounds} — "
                    f"{current.display_name.upper()} — {active['candidate_id']}",
                    response,
                )

                print(
                    f"\nCounter-round {completed_rounds}: "
                    f"{current.display_name} produced {active['candidate_id']}"
                )

                current = (
                    self.momus if current is self.prometheus else self.prometheus
                )

            # Round limit.
            assert active is not None
            self.write_no_consensus(active, completed_rounds)
            outcome = "no_consensus"
            self.write_manifest(outcome, active)
            self.publish_run()
            self.write_status(
                state="no_consensus",
                stage="complete",
                run_dir=str(self.run_dir),
                latest_candidate=active["candidate_id"],
            )
            self._success = True

            print("\n" + "=" * 72)
            print(" NO CONSENSUS REACHED")
            print("=" * 72)
            print(f"Run archive: {self.run_dir}")
            print(f"Report:      {self.run_dir / 'NO_CONSENSUS.md'}")

        except BaseException as exc:
            # Preserve partial work professionally.
            try:
                (self.private_dir / "ERROR.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
                self.write_manifest("failed_or_interrupted", active)
                if not self.run_dir.exists():
                    self.publish_run()
                self.write_status(
                    state="failed_or_interrupted",
                    stage="aborted",
                    run_dir=str(self.run_dir),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as publish_exc:
                print(
                    f"Warning: failed to archive partial run: {publish_exc}",
                    file=sys.stderr,
                )
            raise

        finally:
            self.release_lock()

            if self.private_dir.exists():
                keep = (
                    self.s.keep_private_runtime_on_success
                    if self._success
                    else self.s.keep_private_runtime_on_failure
                )
                if keep:
                    print(
                        f"Private runtime retained at: {self.private_dir}",
                        file=sys.stderr,
                    )
                else:
                    shutil.rmtree(self.private_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Codex agent
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        controller: DebateController,
        machine_name: str,
        display_name: str,
    ):
        self.c = controller
        self.machine_name = machine_name
        self.display_name = display_name
        self.thread_id: Optional[str] = None

    def build_command(self, outfile: Path) -> list[str]:
        s = self.c.s

        cmd = [
            "codex",
            "exec",
            "-C",
            str(s.project_root),
            "--sandbox",
            s.sandbox,
            "--json",
            "--output-schema",
            str(s.schema_file),
            "--output-last-message",
            str(outfile),
        ]

        if s.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")

        if s.ignore_user_config:
            cmd.append("--ignore-user-config")

        if s.ignore_rules:
            cmd.append("--ignore-rules")

        if s.model:
            cmd.extend(["--model", s.model])

        if s.reasoning_effort:
            cmd.extend(
                [
                    "--config",
                    f'model_reasoning_effort="{s.reasoning_effort}"',
                ]
            )

        if s.web_search != "inherit":
            cmd.extend(
                [
                    "--config",
                    f'web_search="{s.web_search}"',
                ]
            )

        if self.thread_id is None:
            cmd.append("-")
        else:
            cmd.extend(["resume", self.thread_id, "-"])

        return cmd

    def run(self, prompt: str, stage: str) -> dict:
        seq = self.c.next_sequence()
        stage_slug = slugify(stage)

        outfile = self.c.private_dir / f"{self.machine_name}_latest.json"
        prompt_log = (
            self.c.private_prompts
            / f"{seq:03d}_{self.machine_name}_{stage_slug}.txt"
        )
        raw_log = (
            self.c.private_raw
            / f"{seq:03d}_{self.machine_name}_{stage_slug}.jsonl"
        )
        response_log = (
            self.c.private_responses
            / f"{seq:03d}_{self.machine_name}_{stage_slug}.json"
        )

        prompt_log.write_text(prompt, encoding="utf-8")
        outfile.parent.mkdir(parents=True, exist_ok=True)

        cmd = self.build_command(outfile)

        print("\n" + "=" * 72)
        print(f" RUNNING {self.display_name.upper()} — {stage}")
        print("=" * 72, flush=True)

        self.c.write_status(
            state="running",
            stage=stage,
            agent=self.display_name,
            prometheus_thread=(
                self.c.prometheus.thread_id if self.c.prometheus else None
            ),
            momus_thread=(self.c.momus.thread_id if self.c.momus else None),
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            start_new_session=True,
        )

        holder: dict[str, object] = {}

        def communicate_worker() -> None:
            try:
                stdout_text, _ = proc.communicate(input=prompt)
                holder["stdout"] = stdout_text
            except BaseException as exc:
                holder["error"] = exc

        worker = threading.Thread(
            target=communicate_worker,
            name=f"{self.machine_name}-codex-communicate",
            daemon=True,
        )
        worker.start()

        started = time.monotonic()
        timeout_seconds = (
            self.c.s.turn_timeout_minutes * 60
            if self.c.s.turn_timeout_minutes > 0
            else None
        )

        try:
            while worker.is_alive():
                worker.join(timeout=self.c.s.heartbeat_seconds)

                elapsed = int(time.monotonic() - started)

                if timeout_seconds is not None and elapsed >= timeout_seconds:
                    print(
                        f"\n{self.display_name} exceeded the configured "
                        f"{self.c.s.turn_timeout_minutes}-minute turn timeout.",
                        file=sys.stderr,
                    )
                    self._terminate_process_group(proc)
                    raise RuntimeError(
                        f"{self.display_name} turn timed out after "
                        f"{self.c.s.turn_timeout_minutes} minutes."
                    )

                if worker.is_alive():
                    minutes, seconds = divmod(elapsed, 60)
                    print(
                        f"[{self.display_name}] still running — "
                        f"elapsed {minutes:02d}:{seconds:02d}",
                        flush=True,
                    )

        except KeyboardInterrupt:
            print(
                f"\nInterrupt received; terminating {self.display_name}...",
                file=sys.stderr,
            )
            self._terminate_process_group(proc)
            raise

        if "error" in holder:
            raise RuntimeError(
                f"Subprocess communication failed for {self.display_name}: "
                f"{holder['error']}"
            )

        stdout_text = str(holder.get("stdout", ""))
        raw_log.write_text(stdout_text, encoding="utf-8")

        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.display_name} failed with exit code {proc.returncode}. "
                f"Raw JSONL: {raw_log}"
            )

        if self.thread_id is None:
            self.thread_id = get_thread_id(stdout_text)
            print(
                f"{self.display_name} persistent thread: {self.thread_id}",
                flush=True,
            )

        if not outfile.exists():
            raise RuntimeError(
                f"{self.display_name} did not produce the structured output file "
                f"{outfile}. Parent directories were pre-created; inspect {raw_log} "
                "and terminal stderr."
            )

        try:
            response = json.loads(outfile.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Could not parse structured response from {self.display_name}: {exc}"
            ) from exc

        response = validate_response(response, self.display_name)
        response_log.write_text(
            json.dumps(response, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        elapsed = int(time.monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        print(
            f"{self.display_name} completed {stage} in "
            f"{minutes:02d}:{seconds:02d}",
            flush=True,
        )

        return response

    @staticmethod
    def _terminate_process_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def run_check(settings: Settings) -> int:
    print("Prometheus-Momus preflight check")
    print("=" * 40)

    problems = []

    if sys.version_info < (3, 9):
        problems.append(
            f"Python 3.9+ is required; found {sys.version.split()[0]}"
        )
    else:
        print(f"[OK] Python: {sys.version.split()[0]}")

    codex = shutil.which("codex")
    if codex:
        print(f"[OK] Codex executable: {codex}")
    else:
        problems.append("`codex` is not available in PATH.")

    for label, path in [
        ("project root", settings.project_root),
        ("task", settings.task_file),
        ("Prometheus role", settings.prometheus_file),
        ("Momus role", settings.momus_file),
        ("schema", settings.schema_file),
        ("config", settings.config_path),
    ]:
        if path.exists():
            print(f"[OK] {label}: {path}")
        else:
            problems.append(f"Missing {label}: {path}")

    if codex:
        try:
            version = subprocess.run(
                ["codex", "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            print(f"[OK] Codex version: {version.stdout.strip() or '(unknown)'}")
        except Exception as exc:
            problems.append(f"Could not run `codex --version`: {exc}")

        try:
            help_run = subprocess.run(
                ["codex", "exec", "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            help_text = help_run.stdout
            required_flags = [
                "--output-schema",
                "--output-last-message",
                "--json",
                "--sandbox",
            ]
            missing_flags = [flag for flag in required_flags if flag not in help_text]
            if missing_flags:
                problems.append(
                    "Codex exec help is missing expected flags: "
                    + ", ".join(missing_flags)
                )
            else:
                print("[OK] Required `codex exec` flags are present.")

            resume_run = subprocess.run(
                ["codex", "exec", "resume", "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            if resume_run.returncode == 0:
                print("[OK] `codex exec resume` is available.")
            else:
                problems.append("`codex exec resume --help` failed.")
        except Exception as exc:
            problems.append(f"Could not inspect Codex exec capabilities: {exc}")

    if "REPLACE THIS" in settings.task_file.read_text(encoding="utf-8"):
        print("[WARN] task.md still contains the template marker `REPLACE THIS`.")

    print()
    print("Effective runtime settings")
    print("-" * 40)
    print(f"Project root:           {settings.project_root}")
    print(f"Min counter-rounds:     {settings.min_counter_rounds}")
    print(f"Max counter-rounds:     {settings.max_counter_rounds}")
    print(f"Blind Momus analysis:   {settings.blind_second_agent}")
    print(f"Final acceptance audit: {settings.final_acceptance_audit}")
    print(f"Model:                  {settings.model or 'inherit'}")
    print(f"Reasoning effort:       {settings.reasoning_effort or 'inherit'}")
    print(f"Web search:             {settings.web_search}")
    print(f"Sandbox:                {settings.sandbox}")

    if problems:
        print("\nFAILED")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print("\nPreflight passed. No model call was made.")
    return 0


def print_effective_config(settings: Settings) -> None:
    data = asdict(settings)
    for key, value in data.items():
        print(f"{key} = {value}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prometheus-Momus: autonomous adversarial orchestration of two "
            "persistent Codex CLI agents."
        )
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to config.ini (default: package config.ini).",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help=(
            "Override the project root configured in config.ini. "
            "Useful when the harness is stored outside the target project."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a local preflight check without making a model call.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the effective parsed configuration and exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Prometheus-Momus {VERSION}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    settings = load_settings(config_path, args.project_root)

    if args.print_config:
        print_effective_config(settings)
        return 0

    if args.check:
        return run_check(settings)

    controller = DebateController(settings)

    try:
        controller.run()
        return 0
    except KeyboardInterrupt:
        print("\nDebate interrupted by user.", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\nFATAL ERROR: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
