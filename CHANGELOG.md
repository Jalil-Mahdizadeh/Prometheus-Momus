# Changelog

## 1.2.0 — 2026-08-26

Autonomous large-debate defaults:

- pinned `gpt-5.6-sol` debate agents at `max` reasoning with live web search;
- independent `gpt-5.5` model adjudication at `xhigh`, making the default
  workflow human-free;
- expanded 4–10 counter-round profile with unlimited model-call, active-wall,
  token, and estimated-cost ceilings by default;
- populated conservative pricing inputs so users can enable cost enforcement
  by setting a single ceiling;
- zero-limit semantics validated, documented, and covered by regression tests.

## 1.1.0 — 2026-08-25

Security, reliability, and portability hardening:

- fail-closed per-agent OS/filesystem/session isolation;
- redacted live status with no transcript path or thread identifiers;
- durable, atomic checkpoints with explicit in-flight replay acknowledgement;
- hard model-call, active-wall-time, and token budgets plus optional
  estimated-cost enforcement;
- structured evidence ledgers with project-file hashing;
- mandatory independent human or heterogeneous-model adjudication;
- crash-atomic, idempotently resumable archive publication;
- deterministic unit/fake-Codex integration tests, executable boundary tests,
  and an opt-in real Codex schema/resume smoke test;
- reusable domain-neutral task and agent-role templates;
- conservative standalone defaults that inherit Codex model/search settings,
  limit target visibility, and bound ordinary runs.

## 1.0.0 — 2026-08-24

Initial public package.

### Included

- persistent Prometheus and Momus Codex threads;
- independent/blind Momus pre-analysis;
- structured JSON-schema protocol;
- configurable minimum/maximum counter-rounds;
- premature-acceptance repair;
- ACCEPT/blocking-issue consistency repair;
- mandatory optional final acceptance audit;
- read-only sandbox default;
- model/reasoning/web-search configuration;
- heartbeat and optional turn timeout;
- unique self-archiving runs;
- raw JSONL, prompt, response, transcript, and manifest audit trail;
- partial-run archival after failure/interruption;
- local preflight checker;
- Linux/HPC documentation.
