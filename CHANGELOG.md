# Changelog

## 1.4.0 — 2026-08-29

Repeatable round-limit continuation:

- added `--resume <run-id> --extra-rounds N` for continuing a terminal
  `NO_CONSENSUS` debate with the same persistent agent sessions;
- retained private no-consensus session state while preserving every completed
  attempt as an immutable `-continuation-<index>` archive;
- kept input invariants and call, wall-time, token, and cost usage cumulative
  across any number of extensions;
- added deterministic integration coverage for repeated extensions followed by
  consensus, plus complete workflow, architecture, configuration, HPC,
  security, and compatibility documentation.

## 1.3.0 — 2026-08-27

Unadjudicated consensus mode:

- added `adjudication.mode = none`, which publishes accepted agent agreement
  as `CONSENSUS_UNADJUDICATED.md` without creating a review packet or invoking
  an adjudicator;
- made `none` the bundled and parser default while retaining opt-in human and
  heterogeneous-model adjudication;
- distinguished unadjudicated agreement from independently approved
  `CONSENSUS.md` throughout terminal state, manifests, tests, and documentation.

## 1.2.0 — 2026-08-26

Autonomous large-debate defaults:

- pinned `gpt-5.6-sol` debate agents at `max` reasoning with live web search;
- independent `gpt-5.5` model adjudication at `xhigh`, making the default
  workflow human-free;
- compact common configuration with 4–10 rounds and unlimited model-call,
  wall-time, token, and cost ceilings by default;
- optional user-supplied price rates rather than stale bundled prices;
- isolation support for the Codex `codex-code-mode-host` companion.

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
