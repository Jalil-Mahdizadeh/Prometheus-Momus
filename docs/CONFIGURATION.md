# Configuration reference

All runtime settings are in `config.ini`. Paths are resolved relative to the
directory containing that file.

The bundled baseline is intentionally portable: it uses the harness directory
as `project_root`, inherits the operator's model, reasoning, and web-search
settings, keeps agents read-only, allows 2–6 counter-rounds, caps execution at
16 model calls / 120 active minutes / 250,000 tokens, and requires human
adjudication. Change these values deliberately for the target task.

## `[debate]`

### `min_counter_rounds`

Completed `COUNTER` rounds required before either agent may return
`ACCEPT`. The opening and blind analysis do not count.

### `max_counter_rounds`

Hard cap on committed counter-rounds. Reaching it publishes
`NO_CONSENSUS.md`.

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

Per-call timeout. `0` disables only this per-call limit; the total wall
budget remains mandatory.

## `[codex]`

### `model` and `reasoning_effort`

Blank values inherit Codex configuration. Explicit values are passed on every
new and resumed call.

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
- `runs_dir`: terminal, immutable audit archives.
- `state_dir`: durable live checkpoints; must be a dedicated narrow
  directory and is hidden from agents.

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

### Mandatory hard ceilings

- `max_model_calls`: includes openings, counters, repairs, audits, and model
  adjudication;
- `max_wall_minutes`: cumulative active controller time across resumes;
- `max_total_tokens`: input plus output tokens reported by Codex.

All three must be positive. A completed call that crosses a token/cost limit
is archived as budget-exhausted before its response can advance the debate.
If token accounting is enabled and Codex emits no parseable terminal usage,
the controller fails closed.

### Optional estimated-cost ceiling

`max_estimated_cost_usd = 0` disables cost enforcement. To enable it, set a
positive ceiling and current model-specific rates:

- `input_usd_per_million`;
- `cached_input_usd_per_million`;
- `output_usd_per_million`.

Input and output rates must be non-zero when the cost ceiling is active. A
zero cached-input rate conservatively uses the full input rate. Rates are
operator-supplied because models and pricing change. The result is an estimate
based on Codex-reported tokens, not a billing statement.

## `[evidence]`

### `require_for_acceptance`

When true, `ACCEPT` requires a non-empty evidence ledger. Disputed evidence
always makes acceptance invalid. Project-file sources must resolve inside the
project and are hashed. Every final-candidate source must still be
independently checked by the adjudicator.

Use project-relative paths, optionally followed by a `#L...` location.

## `[adjudication]`

### `mode = human`

Default. Agent acceptance creates `ADJUDICATION_REQUEST.md` and a JSON
template but does not publish consensus. Finalize with:

```bash
python3 debate.py --adjudicate <run-id> --review-file review.json \
  --reviewer "name-or-auditable-id"
```

### `mode = model`

Runs an independent judge. `model` must be explicit and different from
`[codex] model`; inherited or identical models are rejected. Use
`reasoning_effort` to set the judge's effort.

An `APPROVE` review must contain a non-contradictory `verified` check with
notes for every evidence source.

## `[output]`

- `publish_prompts`: include exact prompts in terminal archives.
- `publish_raw_jsonl`: include Codex event streams.
- `keep_private_runtime_on_success`: retain private state after terminal
  archival.
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
