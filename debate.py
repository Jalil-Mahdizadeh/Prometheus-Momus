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
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from controller_safety import (
    BudgetExceeded,
    BudgetTracker,
    CheckpointStore,
    atomic_write_json,
    audit_evidence,
    parse_codex_usage,
    required_independent_sources,
    validate_adjudication,
)
from runtime_isolation import IsolationManager


VERSION = "1.1.0"
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "config.ini"


class TerminationRequested(KeyboardInterrupt):
    def __init__(self, signal_number: int):
        super().__init__(f"received signal {signal_number}")
        self.signal_number = signal_number


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
        "evidence",
    }

    if not isinstance(response, dict):
        raise RuntimeError(f"{agent_name} response is not a JSON object.")

    missing = required - set(response)
    extra = set(response) - required
    if missing:
        raise RuntimeError(
            f"{agent_name} response is missing required fields: {sorted(missing)}"
        )
    if extra:
        raise RuntimeError(
            f"{agent_name} response has unexpected fields: {sorted(extra)}"
        )

    if response["decision"] not in {"PROPOSE", "COUNTER", "ACCEPT"}:
        raise RuntimeError(
            f"{agent_name} returned invalid decision: {response['decision']!r}"
        )

    if not isinstance(response["critique"], list) or not all(
        isinstance(item, str) for item in response["critique"]
    ):
        raise RuntimeError(f"{agent_name}.critique must be an array/list.")
    if not isinstance(response["blocking_issues"], list) or not all(
        isinstance(item, str) for item in response["blocking_issues"]
    ):
        raise RuntimeError(f"{agent_name}.blocking_issues must be an array/list.")
    if not isinstance(response["proposal"], str) or not response["proposal"].strip():
        raise RuntimeError(f"{agent_name}.proposal must be a non-empty string.")
    if not isinstance(response["rationale"], str) or not response["rationale"].strip():
        raise RuntimeError(f"{agent_name}.rationale must be a non-empty string.")
    if not isinstance(response["evidence"], list):
        raise RuntimeError(f"{agent_name}.evidence must be an array/list.")

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

    if response["evidence"]:
        chunks.append("\n## Evidence ledger\n")
        for item in response["evidence"]:
            chunks.append(
                "- [{status}] {claim} — {source} ({source_type})".format(**item)
            )

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
    adjudication_schema_file: Path
    runs_dir: Path
    state_dir: Path

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

    isolation_enabled: bool
    isolation_backend: str
    isolation_extra_read_paths: tuple[Path, ...]

    max_model_calls: int
    max_wall_minutes: int
    max_total_tokens: int
    max_estimated_cost_usd: float
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float

    require_evidence_for_acceptance: bool
    adjudication_mode: str
    adjudicator_model: str
    adjudicator_reasoning_effort: str

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
    momus_file = resolve_from(package_dir, parser["paths"].get("momus_file", "Momus.md"))
    schema_file = resolve_from(package_dir, parser["paths"].get("schema_file", "schema.json"))
    adjudication_schema_file = resolve_from(
        package_dir,
        parser["paths"].get("adjudication_schema_file", "adjudication_schema.json"),
    )
    runs_dir = resolve_from(package_dir, parser["paths"].get("runs_dir", "runs"))
    state_dir = resolve_from(
        package_dir,
        parser["paths"].get("state_dir", ".prometheus-momus-state"),
    )

    isolation = parser["isolation"] if parser.has_section("isolation") else {}
    budget = parser["budget"] if parser.has_section("budget") else {}
    evidence = parser["evidence"] if parser.has_section("evidence") else {}
    adjudication = (
        parser["adjudication"] if parser.has_section("adjudication") else {}
    )

    try:
        min_rounds = int(parser["debate"].get("min_counter_rounds", "3"))
        max_rounds = int(parser["debate"].get("max_counter_rounds", "8"))
        max_repairs = int(parser["debate"].get("max_protocol_repairs", "2"))
        heartbeat = int(parser["debate"].get("heartbeat_seconds", "60"))
        timeout = int(parser["debate"].get("turn_timeout_minutes", "0"))

        isolation_enabled = as_bool(
            isolation.get("enabled", "true"), key="isolation.enabled"
        )
        max_model_calls = int(budget.get("max_model_calls", "40"))
        max_wall_minutes = int(budget.get("max_wall_minutes", "360"))
        max_total_tokens = int(budget.get("max_total_tokens", "1500000"))
        max_estimated_cost_usd = float(
            budget.get("max_estimated_cost_usd", "0")
        )
        input_usd_per_million = float(
            budget.get("input_usd_per_million", "0")
        )
        cached_input_usd_per_million = float(
            budget.get("cached_input_usd_per_million", "0")
        )
        output_usd_per_million = float(
            budget.get("output_usd_per_million", "0")
        )
        require_evidence = as_bool(
            evidence.get("require_for_acceptance", "true"),
            key="evidence.require_for_acceptance",
        )

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
    if not keep_failure:
        die(
            "output.keep_private_runtime_on_failure must be true because "
            "durable resume state cannot be discarded"
        )
    if max_model_calls < 1:
        die("budget.max_model_calls must be >= 1")
    if max_wall_minutes < 1:
        die("budget.max_wall_minutes must be >= 1")
    if max_total_tokens < 1:
        die("budget.max_total_tokens must be >= 1")
    rates = (
        max_estimated_cost_usd,
        input_usd_per_million,
        cached_input_usd_per_million,
        output_usd_per_million,
    )
    if any(value < 0 for value in rates):
        die("Budget and price values must be >= 0")
    if max_estimated_cost_usd and not (
        input_usd_per_million and output_usd_per_million
    ):
        die(
            "A cost ceiling requires non-zero input and output rates"
        )

    sandbox = parser["codex"].get("sandbox", "read-only").strip() or "read-only"
    allowed_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
    if sandbox not in allowed_sandboxes:
        die(
            f"sandbox must be one of {sorted(allowed_sandboxes)}, got {sandbox!r}"
        )
    if blind and sandbox != "read-only":
        die(
            "blind_second_agent=true requires codex.sandbox=read-only so the "
            "opening agent cannot leak its response through the shared project"
        )
    if sandbox != "read-only":
        control_paths = (
            PACKAGE_DIR,
            config_path.resolve(),
            task_file,
            prometheus_file,
            momus_file,
            schema_file,
            adjudication_schema_file,
        )
        exposed_controls = []
        for path in control_paths:
            try:
                path.resolve().relative_to(project_root)
            except ValueError:
                continue
            exposed_controls.append(str(path))
        if exposed_controls:
            die(
                "Write-capable mode requires the harness, config, task, roles, "
                "and schemas to live outside project_root. Exposed controls: "
                + ", ".join(exposed_controls)
            )

    web_search = parser["codex"].get("web_search", "inherit").strip().lower() or "inherit"
    # Codex CLI has evolved over time; keep validation conservative but allow
    # the currently used modes and a no-override setting.
    if web_search not in {"inherit", "disabled", "cached", "indexed", "live"}:
        die(
            "web_search must be inherit, disabled, cached, indexed, or live; "
            f"got {web_search!r}"
        )

    isolation_backend = isolation.get("backend", "auto").strip().lower() or "auto"
    if isolation_backend not in {"auto", "bubblewrap", "sandbox-exec"}:
        die("isolation.backend must be auto, bubblewrap, or sandbox-exec")
    if blind and not isolation_enabled:
        die(
            "blind_second_agent=true requires isolation.enabled=true; "
            "blindness now fails closed"
        )
    if blind and (
        platform.system() != "Linux" or isolation_backend == "sandbox-exec"
    ):
        die(
            "Enforced blind mode currently requires Linux with bubblewrap; "
            "macOS sandbox-exec is not treated as an equivalent security boundary"
        )
    extra_paths_raw = isolation.get("extra_read_paths", "")
    isolation_extra_read_paths = tuple(
        resolve_from(package_dir, item.strip())
        for line in extra_paths_raw.splitlines()
        for item in line.split(",")
        if item.strip()
    )

    adjudication_mode = adjudication.get("mode", "human").strip().lower() or "human"
    adjudicator_model = adjudication.get("model", "").strip()
    adjudicator_reasoning = adjudication.get("reasoning_effort", "high").strip()
    if adjudication_mode not in {"human", "model"}:
        die("adjudication.mode must be human or model")
    debate_model = parser["codex"].get("model", "").strip()
    if adjudication_mode == "model":
        if not debate_model or not adjudicator_model:
            die(
                "Model adjudication requires explicit codex.model and "
                "adjudication.model values"
            )
        if adjudicator_model == debate_model:
            die("The adjudicator model must differ from the debate model")

    if not project_root.exists() or not project_root.is_dir():
        die(f"Project root does not exist or is not a directory: {project_root}")
    if state_dir in {Path("/"), Path.home().resolve(), project_root}:
        die("paths.state_dir must be a dedicated, narrow directory")
    if state_dir in project_root.parents:
        die("paths.state_dir cannot contain the project root")
    if (
        state_dir == runs_dir
        or state_dir in runs_dir.parents
        or runs_dir in state_dir.parents
    ):
        die("paths.state_dir and paths.runs_dir must not overlap")

    return Settings(
        config_path=config_path.resolve(),
        project_root=project_root,
        task_file=task_file,
        prometheus_file=prometheus_file,
        momus_file=momus_file,
        schema_file=schema_file,
        adjudication_schema_file=adjudication_schema_file,
        runs_dir=runs_dir,
        state_dir=state_dir,

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

        isolation_enabled=isolation_enabled,
        isolation_backend=isolation_backend,
        isolation_extra_read_paths=isolation_extra_read_paths,

        max_model_calls=max_model_calls,
        max_wall_minutes=max_wall_minutes,
        max_total_tokens=max_total_tokens,
        max_estimated_cost_usd=max_estimated_cost_usd,
        input_usd_per_million=input_usd_per_million,
        cached_input_usd_per_million=cached_input_usd_per_million,
        output_usd_per_million=output_usd_per_million,

        require_evidence_for_acceptance=require_evidence,
        adjudication_mode=adjudication_mode,
        adjudicator_model=adjudicator_model,
        adjudicator_reasoning_effort=adjudicator_reasoning,

        publish_prompts=publish_prompts,
        publish_raw_jsonl=publish_raw,
        keep_private_runtime_on_success=keep_success,
        keep_private_runtime_on_failure=keep_failure,
    )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class DebateController:
    def __init__(
        self,
        settings: Settings,
        *,
        resume_id: Optional[str] = None,
        retry_inflight: bool = False,
    ):
        self.s = settings
        if resume_id is not None and (
            not resume_id
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for character in resume_id
            )
        ):
            die(f"Invalid resume run ID: {resume_id!r}")
        self.run_id = (
            resume_id
            if resume_id is not None
            else datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self.resuming = resume_id is not None
        self.retry_inflight = retry_inflight

        self.private_dir = self.s.state_dir / self.run_id
        self.private_prompts = self.private_dir / "prompts"
        self.private_raw = self.private_dir / "raw-jsonl"
        self.private_responses = self.private_dir / "responses"
        self.private_transcript = self.private_dir / "DEBATE_TRANSCRIPT.md"
        self.private_history = self.private_dir / "history.jsonl"
        self.private_blind = self.private_dir / "momus_blind_analysis.json"
        self.private_evidence = self.private_dir / "evidence_audit.jsonl"
        self.checkpoints = CheckpointStore(self.private_dir / "checkpoint.json")

        self.run_dir = self.s.runs_dir / self.run_id
        self.status_file = PACKAGE_DIR / "RUN_STATUS.json"
        self.latest_file = PACKAGE_DIR / "LATEST_RUN.txt"
        self.lock_path = PACKAGE_DIR / ".debate.lock"

        self._sequence = 0
        self._lock_handle = None
        self._terminal = False

        self.task_text = self._read_required(self.s.task_file, "task")
        self.prometheus_role = self._read_required(
            self.s.prometheus_file, "Prometheus role"
        )
        self.momus_role = self._read_required(self.s.momus_file, "Momus role")
        self.schema = self._load_schema()
        self.adjudication_schema = self._load_json(
            self.s.adjudication_schema_file, "adjudication schema"
        )

        self.prometheus: Optional[Agent] = None
        self.momus: Optional[Agent] = None

        if self.resuming:
            self.state = self.checkpoints.load()
            if self.state.get("run_id") != self.run_id:
                die("Checkpoint run ID does not match the requested run")
            self._verify_resume_inputs()
            expected_hashes = self.state.get("input_sha256")
            assert isinstance(expected_hashes, dict)
            self.original_input_hashes = dict(expected_hashes)
            inflight = self.state.get("inflight")
            if inflight and not self.retry_inflight:
                raise RuntimeError(
                    "The checkpoint records an interrupted model call. Resume "
                    "with --retry-inflight only after accepting that the last "
                    "prompt may be replayed in the persistent thread."
                )
            self._sequence = int(self.state.get("sequence", 0))
        else:
            if self.private_dir.exists():
                die(f"Refusing to reuse existing controller state: {self.private_dir}")
            self.state = {
                "run_id": self.run_id,
                "phase": "new",
                "active": None,
                "completed_rounds": 0,
                "next_agent": "momus",
                "acceptance": None,
                "inflight": None,
            }
            self.original_input_hashes = self._input_hashes()

        restored_budget = (
            self.state.get("budget") if isinstance(self.state.get("budget"), dict) else None
        )
        self.budget = BudgetTracker(
            max_calls=self.s.max_model_calls,
            max_wall_seconds=self.s.max_wall_minutes * 60,
            max_total_tokens=self.s.max_total_tokens,
            max_estimated_cost_usd=self.s.max_estimated_cost_usd,
            input_usd_per_million=self.s.input_usd_per_million,
            cached_input_usd_per_million=self.s.cached_input_usd_per_million,
            output_usd_per_million=self.s.output_usd_per_million,
            restored=restored_budget,
        )
        self.isolation = IsolationManager(
            enabled=self.s.isolation_enabled,
            backend=self.s.isolation_backend,
            project_root=self.s.project_root,
            controller_state_root=self.s.state_dir,
            run_private_dir=self.private_dir,
            extra_read_paths=self.s.isolation_extra_read_paths,
        )

    @staticmethod
    def _read_required(path: Path, label: str) -> str:
        if not path.exists():
            die(f"Missing {label} file: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            die(f"{label.capitalize()} file is empty: {path}")
        return text

    def _load_schema(self) -> dict:
        return self._load_json(self.s.schema_file, "schema")

    @staticmethod
    def _load_json(path: Path, label: str) -> dict:
        if not path.exists():
            die(f"Missing {label} file: {path}")
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            die(f"Invalid {label} JSON: {exc}")
        if not isinstance(schema, dict):
            die(f"{label.capitalize()} must contain a JSON object: {path}")
        return schema

    def _input_hashes(self) -> dict[str, str]:
        hashes = {
            "task": sha256_file(self.s.task_file),
            "prometheus_role": sha256_file(self.s.prometheus_file),
            "momus_role": sha256_file(self.s.momus_file),
            "schema": sha256_file(self.s.schema_file),
            "adjudication_schema": sha256_file(self.s.adjudication_schema_file),
            "config": sha256_file(self.s.config_path),
        }
        path_identity = [
            str(path)
            for path in (
                self.s.project_root,
                self.s.config_path,
                self.s.task_file,
                self.s.prometheus_file,
                self.s.momus_file,
                self.s.schema_file,
                self.s.adjudication_schema_file,
            )
        ]
        hashes["effective_paths"] = hashlib.sha256(
            json.dumps(path_identity, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return hashes

    def _verify_resume_inputs(self) -> None:
        expected = self.state.get("input_sha256")
        actual = self._input_hashes()
        if expected != actual:
            raise RuntimeError(
                "Task, role, schema, or configuration inputs changed since "
                "this checkpoint; refusing a non-reproducible resume."
            )

    def save_checkpoint(self, **updates: object) -> None:
        if self._input_hashes() != self.original_input_hashes:
            raise RuntimeError(
                "Task, role, schema, or configuration input changed during "
                "the run; refusing to checkpoint mixed inputs."
            )
        self.state.update(updates)
        self.state["run_id"] = self.run_id
        self.state["sequence"] = self._sequence
        self.state["budget"] = self.budget.snapshot()
        self.state["input_sha256"] = self.original_input_hashes
        self.state["threads"] = {
            "prometheus": self.prometheus.thread_id if self.prometheus else None,
            "momus": self.momus.thread_id if self.momus else None,
        }
        self.checkpoints.save(self.state)

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
        self.s.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        shutil.copy2(self.s.momus_file, self.private_dir / "Momus.snapshot.md")
        shutil.copy2(self.s.schema_file, self.private_dir / "schema.snapshot.json")
        shutil.copy2(
            self.s.adjudication_schema_file,
            self.private_dir / "adjudication_schema.snapshot.json",
        )
        shutil.copy2(self.s.config_path, self.private_dir / "config.snapshot.ini")
        self.save_checkpoint(phase="initialized", inflight=None)

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
            "isolation": self.isolation.describe(),
            "budget": self.budget.snapshot(),
            **extra,
        }
        atomic_write_json(self.status_file, payload)

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
            "evidence": response["evidence"],
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
            "evidence": response["evidence"],
        }

    # ------------------------------------------------------------------
    # Prompt protocol
    # ------------------------------------------------------------------

    @property
    def common_protocol(self) -> str:
        return """
AUTONOMOUS DEBATE PROTOCOL

The task below is authoritative.

The JSON field named `proposal` means the COMPLETE CURRENT CANDIDATE, WORK
PRODUCT, PLAN, DESIGN, ANSWER, OR RECOMMENDATION appropriate to the task. It
does not require a particular artifact type.

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
12. Populate the evidence ledger for every factual claim that materially
    affects the proposal. Mark uncertainty honestly. A URL is not considered
    independently verified merely because it was retrieved by this agent.
    Use project-relative paths (optionally followed by #L...) for project_file
    sources.
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
Your previous ACCEPT was inconsistent with the acceptance contract: it had
blocking issues, omitted the required evidence ledger, or relied on disputed
evidence.

Current candidate:

{json.dumps(active, indent=2, ensure_ascii=False)}

Re-evaluate it.

Either:
1. return ACCEPT with blocking_issues=[], a complete evidence ledger, and no
   disputed evidence if no material issue remains; or
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
substantive conclusion merely to satisfy the protocol label.
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
        def inconsistent(value: dict) -> bool:
            if value["decision"] != "ACCEPT":
                return False
            if value["blocking_issues"]:
                return True
            if self.s.require_evidence_for_acceptance and not value["evidence"]:
                return True
            return any(
                item.get("status") == "disputed" for item in value["evidence"]
            )

        while inconsistent(response):
            if repairs >= self.s.max_protocol_repairs:
                raise RuntimeError(
                    f"{agent.display_name} repeatedly returned an ACCEPT that "
                    "violated the blocking-issue/evidence contract."
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
                        "adjudication_schema_file",
                        "runs_dir",
                        "state_dir",
                    }
                },
                "model": self.s.model or "inherit",
                "reasoning_effort": self.s.reasoning_effort or "inherit",
            },
            "prometheus_thread": self.prometheus.thread_id if self.prometheus else None,
            "momus_thread": self.momus.thread_id if self.momus else None,
            "final_candidate_id": active.get("candidate_id") if active else None,
            "final_candidate_author": active.get("author") if active else None,
            "budget_usage": self.budget.snapshot(),
            "isolation": self.isolation.describe(),
            "adjudication_mode": self.s.adjudication_mode,
        }
        (self.private_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def publish_run(self) -> None:
        self.s.runs_dir.mkdir(parents=True, exist_ok=True)
        if self.run_dir.exists():
            manifest_path = self.run_dir / "run_manifest.json"
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Run directory already exists but is not reusable: {self.run_dir}"
                ) from exc
            if existing.get("run_id") != self.run_id:
                raise RuntimeError(f"Run directory belongs to another run: {self.run_dir}")
            self.latest_file.write_text(
                str(self.run_dir.resolve()) + "\n",
                encoding="utf-8",
            )
            return

        staging = self.s.runs_dir / (
            f".{self.run_id}.{uuid.uuid4().hex}.publishing"
        )
        staging.mkdir()
        try:
            for path in self.private_dir.iterdir():
                if path.is_dir():
                    continue
                shutil.copy2(path, staging / path.name)

            if self.s.publish_prompts and self.private_prompts.exists():
                shutil.copytree(self.private_prompts, staging / "prompts")

            if self.s.publish_raw_jsonl and self.private_raw.exists():
                shutil.copytree(self.private_raw, staging / "raw-jsonl")

            if self.private_responses.exists():
                shutil.copytree(self.private_responses, staging / "responses")

            os.replace(staging, self.run_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

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
        adjudication: dict,
        reviewer: str,
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
- Independently approved by: `{reviewer}`
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

## Independent Adjudication

{adjudication['rationale']}

## Interpretation

Consensus means that the two persistent agents could no longer justify a
material improvement under the configured protocol and that an independent
adjudicator approved the result.

Consensus is not empirical validation, legal advice, proof of correctness,
or proof that external research was exhaustive.
"""
        (self.private_dir / "CONSENSUS.md").write_text(text, encoding="utf-8")
        (self.private_dir / "final_candidate.json").write_text(
            json.dumps(active, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_rejected(
        self,
        active: dict,
        review: dict,
        reviewer: str,
        rounds: int,
    ) -> None:
        issues = review["blocking_issues"]
        issue_text = (
            "\n".join(f"- {item}" for item in issues)
            if issues
            else "The adjudicator rejected the candidate without enumerating blockers."
        )
        text = f"""# Prometheus–Momus Debate — Rejected

## Status

**REJECTED BY INDEPENDENT ADJUDICATOR**

- Run ID: `{self.run_id}`
- Candidate: `{active['candidate_id']}`
- Candidate author: `{active['author']}`
- Adjudicator: `{reviewer}`
- Completed counter-rounds: `{rounds}`

## Candidate

{active['proposal']}

## Adjudication Rationale

{review['rationale']}

## Blocking Issues

{issue_text}

## Interpretation

The debating agents tentatively agreed, but the independent gate did not.
Do not treat this run as consensus.
"""
        (self.private_dir / "REJECTED.md").write_text(text, encoding="utf-8")
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

    def _restore_agents(self) -> None:
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
        threads = self.state.get("threads")
        if isinstance(threads, dict):
            prometheus_thread = threads.get("prometheus")
            momus_thread = threads.get("momus")
            if prometheus_thread:
                self.prometheus.thread_id = str(prometheus_thread)
            if momus_thread:
                self.momus.thread_id = str(momus_thread)

    def _print_banner(self) -> None:
        action = "RESUMING" if self.resuming else "STARTING"
        print("\n" + "=" * 72)
        print(f" {action} PROMETHEUS–MOMUS DEBATE")
        print("=" * 72)
        print(f"Run ID:                  {self.run_id}")
        print(f"Project root:            {self.s.project_root}")
        print(f"Model override:          {self.s.model or 'inherit'}")
        print(f"Reasoning effort:        {self.s.reasoning_effort or 'inherit'}")
        print(f"Web search:              {self.s.web_search}")
        print(f"Codex sandbox:           {self.s.sandbox}")
        print(f"Outer isolation:         {self.isolation.describe()}")
        print(f"Model-call budget:       {self.s.max_model_calls}")
        print(f"Wall-time budget:        {self.s.max_wall_minutes} minutes")
        print(f"Token budget:            {self.s.max_total_tokens}")
        print(f"Independent gate:        {self.s.adjudication_mode}")
        print(f"Private live transcript: {self.private_transcript}")
        print("\nMonitor in another shell with:")
        print(f"  tail -f '{self.private_transcript}'\n")

    def _evidence_audit_records(self) -> list[dict]:
        records: list[dict] = []
        if not self.private_evidence.exists():
            return records
        for line in self.private_evidence.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _write_adjudication_request(
        self,
        active: dict,
        acceptance: dict,
    ) -> None:
        active_sources = {
            item["source"] for item in active.get("evidence", [])
            if isinstance(item, dict) and isinstance(item.get("source"), str)
        }
        relevant_audit = [
            record
            for record in self._evidence_audit_records()
            if record.get("source") in active_sources
        ]
        request = {
            "run_id": self.run_id,
            "task": self.task_text,
            "candidate": active,
            "tentative_acceptance": acceptance,
            "evidence_audit": relevant_audit,
            "instruction": (
                "Independently verify material claims and approve only if the "
                "candidate survives review. Agent agreement is not evidence."
            ),
        }
        atomic_write_json(self.private_dir / "adjudication_request.json", request)

        required_sources = required_independent_sources(active.get("evidence", []))
        template = {
            "decision": "REJECT",
            "rationale": "Replace with an independent review rationale.",
            "blocking_issues": ["Replace or remove; APPROVE requires an empty array."],
            "evidence_checks": [
                {
                    "source": source,
                    "result": "not_checked",
                    "notes": "Verify independently, then update result and notes.",
                }
                for source in sorted(required_sources)
            ],
        }
        atomic_write_json(self.private_dir / "adjudication_template.json", template)

        markdown = f"""# Independent Adjudication Request

- Run ID: `{self.run_id}`
- Candidate: `{active['candidate_id']}`
- Tentatively accepted by: `{acceptance['display_name']}`

Agent agreement is not evidence. Inspect the project and independently verify
every evidence source before approving.

## Candidate

{active['proposal']}

## Evidence Ledger

```json
{json.dumps(active.get('evidence', []), indent=2, ensure_ascii=False)}
```

## Mechanically Generated Evidence Audit

```json
{json.dumps(relevant_audit, indent=2, ensure_ascii=False)}
```

Complete `adjudication_template.json`, then run:

```bash
python3 debate.py --adjudicate {self.run_id} --review-file /path/to/review.json \
  --reviewer "name-or-auditable-id"
```
"""
        (self.private_dir / "ADJUDICATION_REQUEST.md").write_text(
            markdown, encoding="utf-8"
        )

    def _adjudication_prompt(self, request: dict) -> str:
        return f"""
You are the independent final adjudicator for a two-agent debate.

You did not participate in the debate. Do not treat agreement, confidence, or
repetition as evidence. Reinspect the project and independently verify every
material source. Look for correlated model blind spots, unsupported claims,
feasibility failures, and conflicts with the authoritative task.

For every source in the candidate ledger, include an
evidence_checks entry with the exact source string and result="verified" only
after independent verification.

Return APPROVE only if no material blocking issue remains. Otherwise return
REJECT and enumerate the blockers.

ADJUDICATION PACKET
===================

{json.dumps(request, indent=2, ensure_ascii=False)}
"""

    def _continue_pending_adjudication(self) -> None:
        active = self.state.get("active")
        acceptance = self.state.get("acceptance")
        if not isinstance(active, dict) or not isinstance(acceptance, dict):
            raise RuntimeError("Pending-adjudication checkpoint is incomplete")

        self._write_adjudication_request(active, acceptance)
        if self.s.adjudication_mode == "human":
            self.write_status(
                state="awaiting_human_adjudication",
                stage="independent_gate",
                final_candidate=active["candidate_id"],
            )
            print("\n" + "=" * 72)
            print(" INDEPENDENT HUMAN ADJUDICATION REQUIRED")
            print("=" * 72)
            print(f"Review packet: {self.private_dir / 'ADJUDICATION_REQUEST.md'}")
            print(f"Template:      {self.private_dir / 'adjudication_template.json'}")
            print("\nConsensus has not been published.")
            return

        request = json.loads(
            (self.private_dir / "adjudication_request.json").read_text(
                encoding="utf-8"
            )
        )
        required_sources = required_independent_sources(active.get("evidence", []))
        judge = Agent(
            controller=self,
            machine_name="adjudicator",
            display_name="Independent adjudicator",
            model_override=self.s.adjudicator_model,
            reasoning_override=self.s.adjudicator_reasoning_effort,
            sandbox_override="read-only",
        )
        review = judge.run(
            self._adjudication_prompt(request),
            "independent_model_adjudication",
            schema_file=self.s.adjudication_schema_file,
            adjudication_sources=required_sources,
        )
        self._finalize_adjudication(
            review,
            reviewer=f"model:{self.s.adjudicator_model}",
        )

    def _complete_publication(self) -> None:
        outcome = str(self.state.get("outcome", "unknown"))
        active = self.state.get("active")
        self.publish_run()
        self.save_checkpoint(phase="terminal", inflight=None)
        shutil.copy2(
            self.checkpoints.path,
            self.run_dir / self.checkpoints.path.name,
        )
        self.write_status(
            state=outcome,
            stage="complete",
            run_dir=str(self.run_dir),
            final_candidate=(
                active.get("candidate_id") if isinstance(active, dict) else None
            ),
        )
        self._terminal = True

    def _finalize_adjudication(self, review: dict, *, reviewer: str) -> None:
        active = self.state.get("active")
        acceptance = self.state.get("acceptance")
        if not isinstance(active, dict) or not isinstance(acceptance, dict):
            raise RuntimeError("Cannot finalize an incomplete adjudication checkpoint")
        if self.prometheus is None or self.momus is None:
            self._restore_agents()

        accepting_agent = (
            self.prometheus
            if acceptance.get("agent") == "prometheus"
            else self.momus
        )
        if accepting_agent is None:
            raise RuntimeError("Accepting agent is unavailable")
        response = acceptance.get("response")
        if not isinstance(response, dict):
            raise RuntimeError("Acceptance response is unavailable")
        rounds = int(acceptance.get("rounds", 0))

        adjudication_record = {
            "reviewer": reviewer,
            "reviewed_at": now_iso(),
            **review,
        }
        atomic_write_json(
            self.private_dir / "ADJUDICATION.json",
            adjudication_record,
        )

        if review["decision"] == "APPROVE":
            outcome = "consensus"
            self.write_consensus(
                active,
                accepting_agent,
                response,
                rounds,
                review,
                reviewer,
            )
            report = self.run_dir / "CONSENSUS.md"
        else:
            outcome = "rejected_by_adjudicator"
            self.write_rejected(active, review, reviewer, rounds)
            report = self.run_dir / "REJECTED.md"

        self.write_manifest(outcome, active)
        self.save_checkpoint(
            phase="publishing",
            outcome=outcome,
            active=active,
            adjudication=adjudication_record,
            inflight=None,
        )
        self._complete_publication()

        print("\n" + "=" * 72)
        print(
            " CONSENSUS APPROVED"
            if outcome == "consensus"
            else " CANDIDATE REJECTED BY INDEPENDENT ADJUDICATOR"
        )
        print("=" * 72)
        print(f"Run archive: {self.run_dir}")
        print(f"Report:      {report}")

    def adjudicate(self, review_file: Path, *, reviewer: str) -> None:
        self.acquire_lock()
        try:
            if self.state.get("phase") != "pending_adjudication":
                raise RuntimeError(
                    f"Run {self.run_id} is not awaiting adjudication "
                    f"(phase={self.state.get('phase')!r})."
                )
            if self.s.adjudication_mode != "human":
                raise RuntimeError("Manual adjudication is only valid in human mode")
            reviewer = reviewer.strip()
            if (
                not reviewer
                or len(reviewer) > 200
                or any(ord(character) < 32 for character in reviewer)
            ):
                raise RuntimeError("Reviewer must be a non-empty auditable identifier")
            self._restore_agents()
            try:
                raw_review = json.loads(review_file.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise RuntimeError(f"Review file not found: {review_file}") from exc
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Review file is invalid JSON: {exc}") from exc

            active = self.state.get("active")
            if not isinstance(active, dict):
                raise RuntimeError("Checkpoint has no active candidate")
            review = validate_adjudication(
                raw_review,
                required_sources=required_independent_sources(
                    active.get("evidence", [])
                ),
            )
            self._finalize_adjudication(
                review,
                reviewer=f"human:{reviewer}",
            )
        except BaseException as exc:
            if self.private_dir.exists():
                try:
                    with (self.private_dir / "ERROR.txt").open(
                        "a", encoding="utf-8"
                    ) as handle:
                        handle.write(f"{now_iso()} {type(exc).__name__}: {exc}\n")
                    self.save_checkpoint(
                        last_error=f"{type(exc).__name__}: {exc}",
                        resumable=True,
                    )
                    self.write_status(
                        state="failed_or_interrupted",
                        stage=str(self.state.get("phase", "unknown")),
                        run_id=self.run_id,
                        resumable=True,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception as checkpoint_exc:
                    print(
                        f"Warning: could not update checkpoint: {checkpoint_exc}",
                        file=sys.stderr,
                    )
            raise
        finally:
            self.release_lock()
            self._cleanup_private_state()

    def _finish_budget_exhausted(
        self,
        exc: BudgetExceeded,
        active: Optional[dict],
    ) -> None:
        text = f"""# Prometheus–Momus Debate — Budget Exhausted

## Status

**NO CONSENSUS: HARD BUDGET EXHAUSTED**

- Run ID: `{self.run_id}`
- Reason: {exc}
- Model calls: {self.budget.calls}
- Total tokens: {self.budget.total_tokens}
- Estimated cost: ${self.budget.estimated_cost_usd:.6f}

No candidate is approved or published as consensus.
"""
        if active is not None:
            text += f"""
## Latest Complete Candidate

- Candidate: `{active['candidate_id']}`
- Author: `{active['author']}`

{active['proposal']}
"""
            (self.private_dir / "final_candidate.json").write_text(
                json.dumps(active, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        (self.private_dir / "BUDGET_EXHAUSTED.md").write_text(
            text, encoding="utf-8"
        )
        self.write_manifest("budget_exhausted", active)
        self.save_checkpoint(
            phase="publishing",
            outcome="budget_exhausted",
            active=active,
            inflight=None,
            last_error=f"{type(exc).__name__}: {exc}",
        )
        self._complete_publication()
        print(f"\nHard budget exhausted. Audit archive: {self.run_dir}")

    def _cleanup_private_state(self) -> None:
        if not self.private_dir.exists():
            return
        if self._terminal and not self.s.keep_private_runtime_on_success:
            shutil.rmtree(self.private_dir, ignore_errors=True)
            return
        print(
            f"Durable controller state retained at: {self.private_dir}",
            file=sys.stderr,
        )

    def run(self) -> None:
        self.acquire_lock()
        active = self.state.get("active")
        if active is not None and not isinstance(active, dict):
            active = None

        try:
            if not self.resuming:
                self.prepare_private_runtime()
            elif not self.private_dir.is_dir():
                raise RuntimeError(f"Controller state is missing: {self.private_dir}")

            self._restore_agents()
            if self.state.get("inflight"):
                print(
                    "WARNING: explicitly retrying a previously interrupted "
                    "model-call stage; the persistent thread may replay it.",
                    file=sys.stderr,
                )
                self.save_checkpoint(inflight=None)

            if self.s.isolation_enabled:
                self.isolation.self_test()
            self._print_banner()

            phase = str(self.state.get("phase", "new"))
            if phase == "terminal":
                raise RuntimeError(f"Run {self.run_id} is already terminal")
            if phase == "publishing":
                self._complete_publication()
                print(f"Completed interrupted publication: {self.run_dir}")
                return
            if phase == "pending_adjudication":
                self._continue_pending_adjudication()
                return

            self.write_status(state="running", stage=phase)

            if phase == "initialized":
                response = self.prometheus.run(
                    self.opening_prompt(), "prometheus_opening"
                )
                response = self.enforce_allowed_decision(
                    self.prometheus,
                    response,
                    {"PROPOSE"},
                    "Prometheus opening",
                )
                active = self.make_candidate(self.prometheus, response)
                self.append_history(
                    "prometheus_opening",
                    self.prometheus,
                    response,
                    active["candidate_id"],
                )
                self.append_transcript(
                    f"PROMETHEUS OPENING — {active['candidate_id']}",
                    response,
                )
                self.save_checkpoint(
                    phase="opening_done",
                    active=active,
                    inflight=None,
                )
                phase = "opening_done"

            if phase == "opening_done":
                if not isinstance(active, dict):
                    active = self.state.get("active")
                if not isinstance(active, dict):
                    raise RuntimeError("Opening checkpoint has no active candidate")
                response = self.momus.run(
                    self.blind_prompt(
                        active if not self.s.blind_second_agent else None
                    ),
                    "momus_blind_analysis",
                )
                response = self.enforce_allowed_decision(
                    self.momus,
                    response,
                    {"PROPOSE"},
                    "Momus independent pre-analysis",
                )
                self.private_blind.write_text(
                    json.dumps(response, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                self.append_history(
                    "momus_blind_analysis", self.momus, response, None
                )
                self.append_transcript(
                    "MOMUS BLIND INDEPENDENT ANALYSIS", response
                )
                self.save_checkpoint(
                    phase="debating",
                    active=active,
                    completed_rounds=0,
                    next_agent="momus",
                    inflight=None,
                )
                phase = "debating"

            if phase != "debating":
                raise RuntimeError(f"Unsupported checkpoint phase: {phase}")

            active = self.state.get("active")
            if not isinstance(active, dict):
                raise RuntimeError("Debate checkpoint has no active candidate")
            completed_rounds = int(self.state.get("completed_rounds", 0))
            next_agent = str(self.state.get("next_agent", "momus"))
            current = self.prometheus if next_agent == "prometheus" else self.momus

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
                        "counter-round after premature-accept repair",
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
                        f"{active['candidate_id']}; running final audit..."
                    )
                    response = current.run(
                        self.final_acceptance_prompt(
                            role, active, completed_rounds
                        ),
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
                        "tentative_acceptance",
                        current,
                        response,
                        active["candidate_id"],
                    )
                    self.append_transcript(
                        f"{current.display_name.upper()} TENTATIVELY ACCEPTS "
                        f"{active['candidate_id']}",
                        response,
                    )
                    acceptance = {
                        "agent": current.machine_name,
                        "display_name": current.display_name,
                        "response": response,
                        "rounds": completed_rounds,
                    }
                    self.save_checkpoint(
                        phase="pending_adjudication",
                        active=active,
                        acceptance=acceptance,
                        completed_rounds=completed_rounds,
                        inflight=None,
                    )
                    self._continue_pending_adjudication()
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
                self.save_checkpoint(
                    phase="debating",
                    active=active,
                    completed_rounds=completed_rounds,
                    next_agent=current.machine_name,
                    inflight=None,
                )

            self.write_no_consensus(active, completed_rounds)
            self.write_manifest("no_consensus", active)
            self.save_checkpoint(
                phase="publishing",
                outcome="no_consensus",
                active=active,
                completed_rounds=completed_rounds,
                inflight=None,
            )
            self._complete_publication()
            print("\n" + "=" * 72)
            print(" NO CONSENSUS REACHED")
            print("=" * 72)
            print(f"Run archive: {self.run_dir}")
            print(f"Report:      {self.run_dir / 'NO_CONSENSUS.md'}")

        except BudgetExceeded as exc:
            self._finish_budget_exhausted(exc, active)
        except BaseException as exc:
            if self.private_dir.exists():
                try:
                    with (self.private_dir / "ERROR.txt").open(
                        "a", encoding="utf-8"
                    ) as handle:
                        handle.write(f"{now_iso()} {type(exc).__name__}: {exc}\n")
                    self.save_checkpoint(
                        last_error=f"{type(exc).__name__}: {exc}",
                        resumable=True,
                    )
                    self.write_status(
                        state="failed_or_interrupted",
                        stage=str(self.state.get("phase", "unknown")),
                        run_id=self.run_id,
                        resumable=True,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception as checkpoint_exc:
                    print(
                        f"Warning: could not update checkpoint: {checkpoint_exc}",
                        file=sys.stderr,
                    )
                print(
                    f"Resume with: python3 debate.py --resume {self.run_id}",
                    file=sys.stderr,
                )
            raise
        finally:
            self.release_lock()
            self._cleanup_private_state()

# ---------------------------------------------------------------------------
# Codex agent
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        controller: DebateController,
        machine_name: str,
        display_name: str,
        *,
        model_override: Optional[str] = None,
        reasoning_override: Optional[str] = None,
        sandbox_override: Optional[str] = None,
    ):
        self.c = controller
        self.machine_name = machine_name
        self.display_name = display_name
        self.thread_id: Optional[str] = None
        self.model_override = model_override
        self.reasoning_override = reasoning_override
        self.sandbox_override = sandbox_override

    def build_command(
        self, outfile: Path, schema_file: Path
    ) -> tuple[list[str], Optional[dict[str, str]]]:
        s = self.c.s
        execution_paths = self.c.isolation.paths(self.machine_name, outfile)
        sandbox = self.sandbox_override or s.sandbox

        cmd = [
            execution_paths.codex,
            "exec",
            "-C",
            execution_paths.project_root,
            "--sandbox",
            sandbox,
            "--json",
            "--output-schema",
            execution_paths.schema_file or str(schema_file),
            "--output-last-message",
            execution_paths.output_file,
        ]

        if s.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")

        if s.ignore_user_config:
            cmd.append("--ignore-user-config")

        if s.ignore_rules:
            cmd.append("--ignore-rules")

        model = self.model_override if self.model_override is not None else s.model
        reasoning = (
            self.reasoning_override
            if self.reasoning_override is not None
            else s.reasoning_effort
        )

        if model:
            cmd.extend(["--model", model])

        if reasoning:
            cmd.extend(
                [
                    "--config",
                    f'model_reasoning_effort="{reasoning}"',
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

        wrapped, environment = self.c.isolation.wrap(
            agent_name=self.machine_name,
            command=cmd,
            schema_file=schema_file,
            project_writable=sandbox != "read-only",
        )
        return wrapped, dict(environment) if environment is not None else None

    def run(
        self,
        prompt: str,
        stage: str,
        *,
        schema_file: Optional[Path] = None,
        adjudication_sources: Optional[set[str]] = None,
    ) -> dict:
        seq = self.c.next_sequence()
        stage_slug = slugify(stage)
        selected_schema = schema_file or self.c.s.schema_file

        output_root = (
            self.c.isolation.agent_dir(self.machine_name)
            if self.c.s.isolation_enabled
            else self.c.private_dir
        )
        outfile = output_root / f"{self.machine_name}_latest.json"
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
        outfile.unlink(missing_ok=True)

        self.c.budget.begin_call()
        self.c.save_checkpoint(
            inflight={
                "agent": self.machine_name,
                "stage": stage,
                "sequence": seq,
                "started_at": now_iso(),
            }
        )
        cmd, environment = self.build_command(outfile, selected_schema)

        print("\n" + "=" * 72)
        print(f" RUNNING {self.display_name.upper()} — {stage}")
        print("=" * 72, flush=True)

        self.c.write_status(
            state="running",
            stage=stage,
            agent=self.display_name,
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            start_new_session=True,
            env=environment,
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
                elapsed_before_wait = time.monotonic() - started
                remaining_wall = self.c.budget.remaining_wall_seconds()
                if remaining_wall is not None and remaining_wall <= 0:
                    self._terminate_process_group(proc)
                    raise BudgetExceeded(
                        f"{self.display_name} exceeded the total wall-time budget."
                    )
                remaining_turn = (
                    timeout_seconds - elapsed_before_wait
                    if timeout_seconds is not None
                    else None
                )
                if remaining_turn is not None and remaining_turn <= 0:
                    self._terminate_process_group(proc)
                    raise RuntimeError(
                        f"{self.display_name} turn timed out after "
                        f"{self.c.s.turn_timeout_minutes} minutes."
                    )
                wait_seconds = float(self.c.s.heartbeat_seconds)
                if remaining_wall is not None:
                    wait_seconds = min(wait_seconds, remaining_wall)
                if remaining_turn is not None:
                    wait_seconds = min(wait_seconds, remaining_turn)
                worker.join(timeout=max(wait_seconds, 0.01))

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

                remaining_wall = self.c.budget.remaining_wall_seconds()
                if remaining_wall is not None and remaining_wall <= 0:
                    self._terminate_process_group(proc)
                    raise BudgetExceeded(
                        f"{self.display_name} exceeded the total wall-time budget."
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
            self._terminate_process_group(proc)
            raise RuntimeError(
                f"Subprocess communication failed for {self.display_name}: "
                f"{holder['error']}"
            )

        stdout_text = str(holder.get("stdout", ""))
        raw_log.write_text(stdout_text, encoding="utf-8")

        usage = parse_codex_usage(stdout_text)
        if proc.returncode != 0:
            if usage is not None:
                self.c.budget.record_usage(usage)
            self.c.save_checkpoint()
            raise RuntimeError(
                f"{self.display_name} failed with exit code {proc.returncode}. "
                f"Raw JSONL: {raw_log}"
            )

        try:
            self.c.budget.record_usage(usage)
        finally:
            self.c.save_checkpoint()

        if self.thread_id is None:
            self.thread_id = get_thread_id(stdout_text)
            self.c.save_checkpoint()
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

        if adjudication_sources is None:
            response = validate_response(response, self.display_name)
            evidence_records = audit_evidence(
                response["evidence"],
                project_root=self.c.s.project_root,
                agent=self.display_name,
                stage=stage,
            )
            with self.c.private_evidence.open("a", encoding="utf-8") as handle:
                for record in evidence_records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            response = validate_adjudication(
                response,
                required_sources=adjudication_sources,
            )
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
        ("adjudication schema", settings.adjudication_schema_file),
        ("config", settings.config_path),
    ]:
        if path.exists():
            print(f"[OK] {label}: {path}")
        else:
            problems.append(f"Missing {label}: {path}")

    for label, path, required_fields in (
        (
            "debate schema",
            settings.schema_file,
            {
                "decision",
                "critique",
                "proposal",
                "blocking_issues",
                "rationale",
                "evidence",
            },
        ),
        (
            "adjudication schema",
            settings.adjudication_schema_file,
            {
                "decision",
                "rationale",
                "blocking_issues",
                "evidence_checks",
            },
        ),
    ):
        try:
            parsed_schema = json.loads(path.read_text(encoding="utf-8"))
            declared_required = set(parsed_schema.get("required", []))
            if not required_fields <= declared_required:
                raise ValueError(
                    "missing required fields: "
                    + ", ".join(sorted(required_fields - declared_required))
                )
            if parsed_schema.get("additionalProperties") is not False:
                raise ValueError("top-level additionalProperties must be false")
            print(f"[OK] Parsed {label}.")
        except Exception as exc:
            problems.append(f"Invalid {label}: {exc}")

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

    try:
        if settings.isolation_enabled:
            with tempfile.TemporaryDirectory(
                prefix="prometheus-momus-check-"
            ) as temporary:
                temporary_root = Path(temporary)
                state_root = temporary_root / "state"
                run_root = state_root / "preflight"
                run_root.mkdir(parents=True)
                isolation = IsolationManager(
                    enabled=True,
                    backend=settings.isolation_backend,
                    project_root=settings.project_root,
                    controller_state_root=state_root,
                    run_private_dir=run_root,
                    extra_read_paths=settings.isolation_extra_read_paths,
                )
                isolation.self_test()
                agent_dir = isolation.prepare_agent("preflight")
                output_file = agent_dir / "unused.json"
                paths = isolation.paths("preflight", output_file)
                command, environment = isolation.wrap(
                    agent_name="preflight",
                    command=[paths.codex, "--version"],
                    schema_file=settings.schema_file,
                    project_writable=False,
                )
                version_check = subprocess.run(
                    command,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                )
                if version_check.returncode != 0:
                    detail = (
                        version_check.stderr.strip()
                        or version_check.stdout.strip()
                        or f"exit {version_check.returncode}"
                    )
                    raise RuntimeError(
                        f"isolated Codex --version failed: {detail}"
                    )
                print(
                    f"[OK] Outer agent isolation: {isolation.describe()} "
                    f"({version_check.stdout.strip()})"
                )
        else:
            print("[WARN] Outer agent isolation is disabled.")
    except Exception as exc:
        problems.append(f"Outer agent isolation failed: {exc}")

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
    print(f"Outer isolation:        {settings.isolation_enabled}")
    print(f"Max model calls:        {settings.max_model_calls}")
    print(f"Max wall minutes:       {settings.max_wall_minutes}")
    print(f"Max total tokens:       {settings.max_total_tokens}")
    print(f"Adjudication:           {settings.adjudication_mode}")

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
    run_selection = parser.add_mutually_exclusive_group()
    run_selection.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Resume a durable checkpointed run.",
    )
    run_selection.add_argument(
        "--adjudicate",
        metavar="RUN_ID",
        help="Finalize a run awaiting independent human adjudication.",
    )
    parser.add_argument(
        "--retry-inflight",
        action="store_true",
        help=(
            "Acknowledge and replay an interrupted in-flight model stage. "
            "Valid only with --resume."
        ),
    )
    parser.add_argument(
        "--review-file",
        default=None,
        help="Human adjudication JSON; required with --adjudicate.",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="Human reviewer name or auditable ID; required with --adjudicate.",
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

    if args.retry_inflight and not args.resume:
        die("--retry-inflight is valid only with --resume")
    if args.adjudicate and not args.review_file:
        die("--adjudicate requires --review-file")
    if args.adjudicate and not args.reviewer:
        die("--adjudicate requires --reviewer")
    if args.review_file and not args.adjudicate:
        die("--review-file is valid only with --adjudicate")
    if args.reviewer and not args.adjudicate:
        die("--reviewer is valid only with --adjudicate")

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_termination(signal_number: int, _frame: object) -> None:
        raise TerminationRequested(signal_number)

    signal.signal(signal.SIGTERM, request_termination)
    try:
        controller = DebateController(
            settings,
            resume_id=args.adjudicate or args.resume,
            retry_inflight=args.retry_inflight,
        )
        if args.adjudicate:
            controller.adjudicate(
                Path(args.review_file).expanduser().resolve(),
                reviewer=args.reviewer,
            )
        else:
            controller.run()
        return 0
    except TerminationRequested as exc:
        print(
            f"\nDebate terminated by signal {exc.signal_number}.",
            file=sys.stderr,
        )
        return 128 + exc.signal_number
    except KeyboardInterrupt:
        print("\nDebate interrupted by user.", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\nFATAL ERROR: {exc}\n", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
