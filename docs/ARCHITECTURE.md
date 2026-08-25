# Architecture

## Control flow

```text
Prometheus opening
        |
Momus independent analysis (opening is hidden)
        |
alternating COUNTER rounds
        |
tentative agent ACCEPT
        |
final falsification audit
        |
independent human or heterogeneous-model adjudication
        |
APPROVE -> CONSENSUS     REJECT -> REJECTED
```

The round ceiling and any enabled hard budgets can terminate earlier without
consensus.

## Three permission layers

1. The Python controller owns checkpoints, prompts, transcripts, budgets, and
   publication.
2. The outer isolation backend controls the host filesystem and process view
   available to each Codex process.
3. The Codex `--sandbox` setting controls model-generated tool permissions
   inside that outer view.

These layers are intentionally independent.

## Agent isolation

Prometheus, Momus, and an optional model adjudicator receive separate
per-agent directories and Codex homes. They never share session storage.

Linux bubblewrap exposes:

```text
/codex                  resolved Codex executable, read-only
/workspace              target project
/output-schema.json     stage schema, read-only
/agent                  only this agent's private directory
/tmp                    private tmpfs
```

The controller state subtree is overlaid with a private tmpfs if it lies
inside `/workspace`. Other host paths are absent unless explicitly allowed.
Linux also separates PID/user/IPC/UTS/cgroup namespaces while sharing the
network needed by Codex.

macOS uses a deny-by-default Seatbelt profile through `sandbox-exec`. It
provides best-effort containment for non-blind runs, but not Linux-equivalent
PID namespaces or an accepted enforced-blind boundary.

## Persistent sessions

A first call uses `codex exec ... -` and records the emitted thread ID.
Later calls use `codex exec ... resume <thread-id> -`. Schema, model,
reasoning, web-search, sandbox, and working-directory options are supplied on
every call.

Separate Codex homes make possession of another agent's thread identifier
insufficient to access that agent's local session state. Live public status
does not expose those identifiers.

## Structured protocol and evidence

`schema.json` constrains debate turns. The controller independently validates
types, allowed decisions, non-empty proposal/rationale fields, and evidence
entries.

Evidence audit behavior:

- project paths are confined to the project and hashed;
- URLs are syntax-checked;
- calculations, experiments, and other sources remain explicitly unverified
  by the controller;
- all final-candidate sources require independent adjudicator checks.

`adjudication_schema.json` is separate so debate decisions cannot be confused
with final approval.

## Durable state machine

Controller state is atomically checkpointed under:

```text
.prometheus-momus-state/<run-id>/checkpoint.json
```

Committed phases are:

```text
initialized -> opening_done -> debating
           -> pending_adjudication -> publishing -> terminal
```

Before every model subprocess, the controller increments the call budget and
writes an `inflight` record. A successful protocol transition clears it.
After interruption, replay is never automatic because the persistent thread
may have advanced.

Input hashes prevent resuming with changed task, role, schema, or config.
Token/cost/wall/call usage is restored from the checkpoint.

## Publication and failure behavior

Terminal artifacts are first assembled in private state. Publication copies
them to a hidden staging directory and atomically renames it to
`runs/<run-id>/`. A `publishing` checkpoint makes this step idempotently
resumable.

- round limit: `NO_CONSENSUS.md`;
- hard budget: `BUDGET_EXHAUSTED.md`;
- independent rejection: `REJECTED.md`;
- independent approval: `CONSENSUS.md`.

Ordinary failures remain private and resumable instead of being mistaken for
a terminal archive.
