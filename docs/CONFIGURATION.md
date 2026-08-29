# Configuration reference

`config.ini` exposes the common runtime choices. Omitted advanced options use
the defaults documented here. Paths are resolved relative to the directory
containing the file.

`[debate]`, `[codex]`, and `[paths]` are required. All other sections are
optional.

The bundled profile is intentionally portable and autonomous: it uses the
harness directory as `project_root`, runs `gpt-5.6-sol` at `max` effort with
live web search, keeps agents read-only, allows 4–10 counter-rounds, and
publishes agreement without independent adjudication. Model-call, wall-time,
token, and estimated-cost ceilings default to unlimited. This keeps the
shipped file short without removing advanced controls.

## `[debate]`

### `min_counter_rounds`

Completed `COUNTER` rounds required before either agent may return
`ACCEPT`. The opening and blind analysis do not count.

### `max_counter_rounds`

Initial cap on committed counter-rounds. Reaching it publishes
`NO_CONSENSUS.md` and retains the private agent sessions. Continue with
`--resume <run-id> --extra-rounds N`; each positive value raises the
cumulative ceiling by that many rounds. The command may be repeated without
changing this configuration value.

### `blind_second_agent`

When true, Momus does not receive Prometheus's opening response. This setting
requires outer isolation and `sandbox = read-only`, and fails closed if the
boundary is unavailable. Enforced blind mode currently requires Linux and
bubblewrap.

### `final_acceptance_audit`

Runs one more falsification turn after tentative agent acceptance.

### `max_protocol_repairs`

Maximum correction calls for invalid decisions, premature acceptance, or an
acceptance that violates the blocking-issue/evidence contract.

### `heartbeat_seconds`

Progress-report interval while a Codex subprocess is active.

### `turn_timeout_minutes`

Per-call timeout. `0` disables this per-call limit; a positive total wall
budget remains in force when configured.

## `[codex]`

### `model` and `reasoning_effort`

Explicit values are passed on every new and resumed call. Blank values remain
supported and inherit Codex configuration, but the bundled profile pins both.

### `web_search`

Harness values are `inherit`, `disabled`, `cached`, `indexed`, and
`live`. Non-inherit values are passed as a Codex config override.

### `sandbox`

One of `read-only`, `workspace-write`, or `danger-full-access`. This is
the inner Codex tool policy; outer OS isolation still limits the visible host
filesystem.

Write-capable modes require blind mode to be off and all controller/semantic
files to live outside `project_root`.

### `skip_git_repo_check`, `ignore_user_config`, `ignore_rules`

Direct Codex CLI controls. Project rules remain enabled by default.

## `[paths]`

- `project_root`: target shown to agents as `/workspace` on Linux. The
  bundled value is `.`; pass `--project-root /path/to/target` or use `..`
  when the harness is embedded directly inside the target.
- `task_file`, `prometheus_file`, `momus_file`: semantic inputs.
- `schema_file`: debate response schema.
- `adjudication_schema_file`: independent-review schema.
- `runs_dir`: immutable per-attempt audit archives. Continuation attempts use
  a `-continuation-<index>` suffix.
- `state_dir`: durable live checkpoints and retained no-consensus agent
  sessions; must be a dedicated narrow directory and is hidden from agents.

Do not place `state_dir` at root, at the home directory, or around the
project root. It must not overlap `runs_dir`.

Set `project_root` to the narrowest directory that contains the inputs agents
need. It defines both their visible workspace and the boundary for
`project_file` evidence.

## `[isolation]`

### `enabled`

Must be true when blind mode is enabled.

### `backend`

- `auto`: bubblewrap on Linux, `sandbox-exec` on macOS;
- `bubblewrap`;
- `sandbox-exec`.

Preflight and run startup perform a real backend self-test.
`sandbox-exec` is best-effort containment for non-blind macOS runs and is not
accepted as equivalent to the Linux blind boundary.

### `extra_read_paths`

Optional comma/newline-separated narrow paths mounted read-only. Root, home,
missing paths, and anything overlapping controller state are rejected.

## `[budget]`

### Optional hard ceilings

- `max_model_calls`: includes openings, counters, repairs, audits, and model
  adjudication;
- `max_wall_minutes`: cumulative active controller time across resumes;
- `max_total_tokens`: input plus output tokens reported by Codex.

Each setting accepts `0` for unlimited or a positive ceiling. The bundled
profile uses `0` for all three. A completed call that crosses an enabled token
or cost limit is archived as budget-exhausted before its response can advance
the debate. If token accounting is enabled and Codex emits no parseable
terminal usage, the controller fails closed.

All limits and accumulated usage carry across round-limit continuations.
`--extra-rounds` changes only the round ceiling; it never resets a safety
budget.

### Optional estimated-cost ceiling

`max_estimated_cost_usd = 0` means unlimited and disables cost enforcement. To
enable it, set a positive ceiling and current model-specific rates:

- `input_usd_per_million`;
- `cached_input_usd_per_million`;
- `output_usd_per_million`.

Input and output rates must be non-zero when the cost ceiling is active. A
zero cached-input rate conservatively uses the full input rate. Price rates are
not shipped because they change; add current rates before enabling a ceiling.
The result is an estimate based on Codex-reported tokens, not a billing
statement.

## `[evidence]`

### `require_for_acceptance`

When true, `ACCEPT` requires a non-empty evidence ledger. Disputed evidence
always makes acceptance invalid. Project-file sources must resolve inside the
project and are hashed. In `human` and `model` modes, every final-candidate
source must also be independently checked by the adjudicator.

Use project-relative paths, optionally followed by a `#L...` location.

## `[adjudication]`

### `mode = none`

Default. Publishes accepted agent agreement as
`CONSENSUS_UNADJUDICATED.md` without creating a review packet or making an
adjudicator model call. The report explicitly states that no independent
review occurred. Omitting the section or leaving `mode` blank also selects
`none`.

### `mode = model`

Runs an independent judge. `model` must be explicit and different from
`[codex] model`; inherited or identical models are rejected. Use
`reasoning_effort` to set the judge's effort.

An `APPROVE` review must contain a non-contradictory `verified` check with
notes for every evidence source.

### `mode = human`

Agent acceptance creates `ADJUDICATION_REQUEST.md` and a JSON template but
does not publish consensus. Finalize with:

```bash
python3 debate.py --adjudicate <run-id> --review-file review.json \
  --reviewer "name-or-auditable-id"
```

## `[output]` (optional)

- `publish_prompts`: include exact prompts in terminal archives.
- `publish_raw_jsonl`: include Codex event streams.
- `keep_private_runtime_on_success`: retain private state after a conclusive
  terminal archive. `NO_CONSENSUS` state is retained regardless so it can be
  continued; a later conclusive outcome uses this setting for normal
  cleanup.
- `keep_private_runtime_on_failure`: must remain true; interrupted state is
  required for durable resume.

## Resume invariants

```bash
python3 debate.py --resume <run-id>
```

Task, roles, schemas, config, and effective project/control paths must retain
their original identities. An in-flight checkpoint refuses automatic replay.
If replay is acceptable:

```bash
python3 debate.py --resume <run-id> --retry-inflight
```

The failed call remains counted against the model-call budget.

For a terminal round-limit result, request additional rounds explicitly:

```bash
python3 debate.py --resume <run-id> --extra-rounds N
```

`N` must be positive. The controller requires a terminal
`outcome=no_consensus` checkpoint, the retained Prometheus and Momus thread
state, and no in-flight replay request. It adds `N` to the completed-round
ceiling and records the continuation in the checkpoint. The same base run ID
is used for every extension; immutable result archives gain
`-continuation-<index>` suffixes.

The command can be repeated after another `NO_CONSENSUS` result. Input-hash
invariants and cumulative resource budgets continue to apply.
