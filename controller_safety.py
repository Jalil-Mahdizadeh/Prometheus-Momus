#!/usr/bin/env python3
"""Durable controller safeguards for Prometheus-Momus."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


class BudgetExceeded(RuntimeError):
    """Raised when another model call would violate a configured budget."""


class EvidenceError(RuntimeError):
    """Raised when a structured evidence ledger is invalid."""


def atomic_write_json(path: Path, payload: object) -> None:
    """Atomically replace a JSON file in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _usage_from_mapping(value: object) -> ModelUsage | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _nonnegative_int(
        value.get("input_tokens", value.get("prompt_tokens", 0))
    )
    output_tokens = _nonnegative_int(
        value.get("output_tokens", value.get("completion_tokens", 0))
    )
    cached = _nonnegative_int(
        value.get("cached_input_tokens", value.get("cached_tokens", 0))
    )
    details = value.get("input_tokens_details")
    if isinstance(details, dict):
        cached = max(cached, _nonnegative_int(details.get("cached_tokens", 0)))
    if input_tokens == 0 and output_tokens == 0:
        total = _nonnegative_int(value.get("total_tokens", 0))
        if total:
            input_tokens = total
    if input_tokens == 0 and output_tokens == 0 and cached == 0:
        return None
    return ModelUsage(input_tokens, min(cached, input_tokens), output_tokens)


def parse_codex_usage(jsonl_text: str) -> ModelUsage | None:
    """Parse usage from terminal Codex JSONL events without double counting."""
    total_input = total_cached = total_output = 0
    found = False
    for line in jsonl_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", "")).lower()
        if not any(word in event_type for word in ("completed", "usage", "done")):
            continue
        candidates: list[object] = [event.get("usage"), event.get("token_usage")]
        turn = event.get("turn")
        if isinstance(turn, dict):
            candidates.extend((turn.get("usage"), turn.get("token_usage")))
        for candidate in candidates:
            usage = _usage_from_mapping(candidate)
            if usage is not None:
                total_input += usage.input_tokens
                total_cached += usage.cached_input_tokens
                total_output += usage.output_tokens
                found = True
                break
    if not found:
        return None
    return ModelUsage(total_input, total_cached, total_output)


class BudgetTracker:
    """Durable resource accounting; a zero limit disables that ceiling."""

    def __init__(
        self,
        *,
        max_calls: int,
        max_wall_seconds: int,
        max_total_tokens: int,
        max_estimated_cost_usd: float,
        input_usd_per_million: float,
        cached_input_usd_per_million: float,
        output_usd_per_million: float,
        restored: dict[str, object] | None = None,
    ) -> None:
        self.max_calls = max_calls
        self.max_wall_seconds = max_wall_seconds
        self.max_total_tokens = max_total_tokens
        self.max_estimated_cost_usd = max_estimated_cost_usd
        self.input_usd_per_million = input_usd_per_million
        self.cached_input_usd_per_million = cached_input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        restored = restored or {}
        self.calls = _nonnegative_int(restored.get("calls", 0))
        self.input_tokens = _nonnegative_int(restored.get("input_tokens", 0))
        self.cached_input_tokens = _nonnegative_int(
            restored.get("cached_input_tokens", 0)
        )
        self.output_tokens = _nonnegative_int(restored.get("output_tokens", 0))
        try:
            self.estimated_cost_usd = max(
                float(restored.get("estimated_cost_usd", 0.0)), 0.0
            )
            self._prior_elapsed = max(
                float(restored.get("elapsed_seconds", 0.0)), 0.0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid restored budget checkpoint") from exc
        self._started = time.monotonic()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def elapsed_seconds(self) -> float:
        return self._prior_elapsed + (time.monotonic() - self._started)

    def remaining_wall_seconds(self) -> float | None:
        if self.max_wall_seconds == 0:
            return None
        return max(self.max_wall_seconds - self.elapsed_seconds(), 0.0)

    def begin_call(self) -> None:
        if self.max_calls and self.calls >= self.max_calls:
            raise BudgetExceeded(
                f"Model-call budget exhausted ({self.calls}/{self.max_calls})."
            )
        if self.max_wall_seconds and self.elapsed_seconds() >= self.max_wall_seconds:
            raise BudgetExceeded(
                "Total wall-time budget exhausted "
                f"({self.elapsed_seconds() / 60:.1f}/"
                f"{self.max_wall_seconds / 60:.1f} minutes)."
            )
        if self.max_total_tokens and self.total_tokens >= self.max_total_tokens:
            raise BudgetExceeded(
                f"Token budget exhausted ({self.total_tokens}/"
                f"{self.max_total_tokens})."
            )
        if (
            self.max_estimated_cost_usd
            and self.estimated_cost_usd >= self.max_estimated_cost_usd
        ):
            raise BudgetExceeded(
                "Estimated-cost budget exhausted "
                f"(${self.estimated_cost_usd:.4f}/"
                f"${self.max_estimated_cost_usd:.4f})."
            )
        self.calls += 1

    def record_usage(self, usage: ModelUsage | None) -> None:
        if usage is None:
            if self.max_total_tokens or self.max_estimated_cost_usd:
                raise BudgetExceeded(
                    "Codex emitted no parseable token usage; fail-closed "
                    "accounting is required by the configured token/cost budget."
                )
            return
        self.input_tokens += usage.input_tokens
        self.cached_input_tokens += usage.cached_input_tokens
        self.output_tokens += usage.output_tokens
        uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
        cached_rate = (
            self.cached_input_usd_per_million
            if self.cached_input_usd_per_million > 0
            else self.input_usd_per_million
        )
        self.estimated_cost_usd += (
            uncached * self.input_usd_per_million
            + usage.cached_input_tokens * cached_rate
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000
        if self.max_total_tokens and self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded(
                "Token budget exceeded at the completed-turn boundary "
                f"({self.total_tokens}/{self.max_total_tokens})."
            )
        if (
            self.max_estimated_cost_usd
            and self.estimated_cost_usd > self.max_estimated_cost_usd
        ):
            raise BudgetExceeded(
                "Estimated-cost budget exceeded at the completed-turn boundary "
                f"(${self.estimated_cost_usd:.4f}/"
                f"${self.max_estimated_cost_usd:.4f})."
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "limits": {
                "max_calls": self.max_calls,
                "max_wall_seconds": self.max_wall_seconds,
                "max_total_tokens": self.max_total_tokens,
                "max_estimated_cost_usd": self.max_estimated_cost_usd,
            },
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_evidence(
    evidence: object,
    *,
    project_root: Path,
    agent: str,
    stage: str,
) -> list[dict[str, object]]:
    """Validate evidence and mechanically verify local project-file claims."""
    if not isinstance(evidence, list):
        raise EvidenceError("evidence must be an array")
    records: list[dict[str, object]] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise EvidenceError(f"evidence[{index}] must be an object")
        required = {"claim", "source", "source_type", "status", "notes"}
        missing = required - set(item)
        extra = set(item) - required
        if missing or extra:
            raise EvidenceError(
                f"evidence[{index}] fields invalid; missing={sorted(missing)}, "
                f"extra={sorted(extra)}"
            )
        for key in ("claim", "source", "notes"):
            if not isinstance(item[key], str):
                raise EvidenceError(f"evidence[{index}].{key} must be a string")
        if not item["claim"].strip() or not item["source"].strip():
            raise EvidenceError(
                f"evidence[{index}] claim and source must be non-empty"
            )
        if item["source_type"] not in {
            "project_file", "url", "calculation", "experiment", "other"
        }:
            raise EvidenceError(f"evidence[{index}] has invalid source_type")
        if item["status"] not in {"verified", "unverified", "disputed"}:
            raise EvidenceError(f"evidence[{index}] has invalid status")
        record: dict[str, object] = {
            "agent": agent,
            "stage": stage,
            "index": index,
            **item,
            "mechanically_verified": False,
            "verification": "",
        }
        if item["source_type"] == "project_file":
            source_path = item["source"].split("#", 1)[0].strip()
            path = Path(source_path).expanduser()
            if path.is_absolute() and path.parts[:2] == ("/", "workspace"):
                path = project_root.joinpath(*path.parts[2:])
            elif not path.is_absolute():
                path = project_root / path
            resolved = path.resolve()
            try:
                resolved.relative_to(project_root.resolve())
            except ValueError as exc:
                raise EvidenceError(
                    f"evidence[{index}] project_file escapes project root"
                ) from exc
            if not resolved.is_file():
                raise EvidenceError(
                    f"evidence[{index}] project_file does not exist: {source_path}"
                )
            record["mechanically_verified"] = True
            record["verification"] = "project file exists and was hashed"
            record["resolved_path"] = str(resolved)
            record["sha256"] = _file_sha256(resolved)
        elif item["source_type"] == "url":
            parsed = urlparse(item["source"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise EvidenceError(f"evidence[{index}] URL is malformed")
            record["verification"] = (
                "URL syntax valid; content requires independent adjudicator verification"
            )
        else:
            record["verification"] = (
                "non-file evidence requires independent adjudicator verification"
            )
        records.append(record)
    return records


def required_independent_sources(
    evidence: Iterable[dict[str, object]],
) -> set[str]:
    return {
        str(item.get("source", ""))
        for item in evidence
        if str(item.get("source", ""))
    }


def validate_adjudication(
    review: object,
    *,
    required_sources: set[str],
) -> dict[str, object]:
    required = {"decision", "rationale", "blocking_issues", "evidence_checks"}
    if not isinstance(review, dict):
        raise ValueError("Adjudication must be a JSON object")
    missing = required - set(review)
    extra = set(review) - required
    if missing or extra:
        raise ValueError(
            f"Adjudication fields invalid; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    if review["decision"] not in {"APPROVE", "REJECT"}:
        raise ValueError("Adjudication decision must be APPROVE or REJECT")
    if not isinstance(review["rationale"], str) or not review["rationale"].strip():
        raise ValueError("Adjudication rationale must be a non-empty string")
    if not isinstance(review["blocking_issues"], list) or not all(
        isinstance(item, str) for item in review["blocking_issues"]
    ):
        raise ValueError("Adjudication blocking_issues must be an array of strings")
    if not isinstance(review["evidence_checks"], list):
        raise ValueError("Adjudication evidence_checks must be an array")
    results_by_source: dict[str, set[str]] = {}
    for index, item in enumerate(review["evidence_checks"]):
        if not isinstance(item, dict) or set(item) != {"source", "result", "notes"}:
            raise ValueError(
                f"evidence_checks[{index}] must contain source, result, notes"
            )
        if not all(isinstance(item[key], str) for key in item):
            raise ValueError(f"evidence_checks[{index}] values must be strings")
        if item["result"] not in {"verified", "rejected", "not_checked"}:
            raise ValueError(f"evidence_checks[{index}] has invalid result")
        if not item["source"].strip() or not item["notes"].strip():
            raise ValueError(
                f"evidence_checks[{index}] source and notes must be non-empty"
            )
        results_by_source.setdefault(item["source"], set()).add(item["result"])
    if review["decision"] == "APPROVE":
        if review["blocking_issues"]:
            raise ValueError("APPROVE is incompatible with blocking_issues")
        incomplete = {
            source
            for source in required_sources
            if results_by_source.get(source) != {"verified"}
        }
        if incomplete:
            raise ValueError(
                "APPROVE requires independent verification of: "
                + ", ".join(sorted(incomplete))
            )
    return review


class CheckpointStore:
    """Versioned, atomic checkpoint persistence."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, payload: dict[str, object]) -> None:
        clean_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"checkpoint_version", "updated_at"}
        }
        atomic_write_json(
            self.path,
            {
                **clean_payload,
                "checkpoint_version": self.VERSION,
                "updated_at": time.time(),
            },
        )

    def load(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Checkpoint not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Checkpoint is invalid JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Checkpoint must be a JSON object")
        if payload.get("checkpoint_version") != self.VERSION:
            raise RuntimeError(
                "Unsupported checkpoint version: "
                f"{payload.get('checkpoint_version')!r}"
            )
        return payload
