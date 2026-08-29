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
configured adjudication mode
        |
        +-> none -> CONSENSUS_UNADJUDICATED
        |
        +-> human or heterogeneous model
                    |
          APPROVE -> CONSENSUS     REJECT -> REJECTED
```

The round ceiling publishes a resumable `NO_CONSENSUS` attempt. An explicit
`--resume <run-id> --extra-rounds N` transition returns that checkpoint to
`debating` with a higher cumulative ceiling; this may be repeated. Enabled
hard budgets can still terminate without consensus and are not bypassed by a
round extension.

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
/codex-code-mode-host   optional adjacent Codex companion, read-only
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

A round-limit outcome retains the session storage required to use the same
persistent threads, including both private Codex homes for isolated runs.
Other successful terminal outcomes follow
the configured private-runtime cleanup policy.

## Structured protocol and evidence

`schema.json` constrains debate turns. The controller independently validates
types, allowed decisions, non-empty proposal/rationale fields, and evidence
entries.

Evidence audit behavior:

- project paths are confined to the project and hashed;
- URLs are syntax-checked;
- calculations, experiments, and other sources remain explicitly unverified
  by the controller;
- human/model approval requires independent checks for all final-candidate
  sources; `none` mode performs no such review.

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

The `terminal` phase is normally final. The deliberate exception is
`outcome=no_consensus`: a positive `--extra-rounds` request records a
continuation event, increments the continuation index, raises the cumulative
`round_limit`, clears only the previous private terminal artifacts, and commits
the phase back to `debating`. The next-agent pointer, active candidate,
sequence number, histories, thread IDs, and resource accounting remain
unchanged.

`pending_adjudication` is the durable tentative-acceptance checkpoint. In
`none` mode the controller resolves it immediately into unadjudicated
publication; human mode can remain there while awaiting review.

Before every model subprocess, the controller increments the call budget and
writes an `inflight` record. A successful protocol transition clears it.
After interruption, replay is never automatic because the persistent thread
may have advanced.

Input hashes prevent resuming with changed task, role, schema, or config.
Token/cost/wall/call usage is restored from the checkpoint.

## Publication and failure behavior

Terminal artifacts are first assembled in private state. Publication copies
them to a hidden staging directory and atomically renames it to
`runs/<run-id>/` for the initial attempt. Continuations publish to
`runs/<run-id>-continuation-<index>/`, preserving every earlier attempt
without rewriting it. Each continuation archive contains the cumulative
transcript and logs. A `publishing` checkpoint makes each publication
idempotently resumable.

- round limit: `NO_CONSENSUS.md`;
- hard budget: `BUDGET_EXHAUSTED.md`;
- agent agreement without adjudication: `CONSENSUS_UNADJUDICATED.md`;
- independent rejection: `REJECTED.md`;
- independent approval: `CONSENSUS.md`.

A round-limit archive is terminal as an audit snapshot but leaves its private
state available for an explicit extension. A later consensus, rejection, or
budget exhaustion follows the normal cleanup setting; prior attempt archives
remain untouched.

Ordinary failures remain private and resumable instead of being mistaken for
a terminal archive.
