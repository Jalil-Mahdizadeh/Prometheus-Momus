# Configuration reference

All runtime settings are in `config.ini`.

## `[debate]`

### `min_counter_rounds`

Number of completed `COUNTER` rounds required before either agent can
legally accept the current candidate.

A value of 3 means:

```text
Prometheus opening
Momus blind analysis
Momus COUNTER   #1
Prometheus COUNTER  #2
Momus COUNTER   #3
acceptance now permitted
```

### `max_counter_rounds`

Hard cap on successful counter-rounds.

If reached, the controller writes `NO_CONSENSUS.md`.

### `blind_second_agent`

When true, Momus does not receive Prometheus's opening response during its
independent pre-analysis.

### `final_acceptance_audit`

When true, a tentative ACCEPT triggers one additional falsification turn
before consensus is recorded.

### `max_protocol_repairs`

Maximum automatic correction attempts for protocol-invalid responses such
as premature acceptance or ACCEPT with blocking issues.

### `heartbeat_seconds`

How frequently the controller prints a "still running" heartbeat.

### `turn_timeout_minutes`

Controller-side timeout for one Codex turn.

`0` disables the timeout.

---

## `[codex]`

### `model`

Blank means inherit the user's Codex configuration.

Set a model name only if you want the harness to pin it.

### `reasoning_effort`

Blank means inherit.

Valid values depend on the selected model and Codex CLI release.

### `web_search`

Supported harness values:

- `inherit`
- `disabled`
- `cached`
- `indexed`
- `live`

The harness passes non-`inherit` values to Codex as a config override.
Because Codex evolves, run `./check.sh` after CLI upgrades.

### `sandbox`

Default:

```ini
sandbox = read-only
```

The harness recognizes:

- `read-only`
- `workspace-write`
- `danger-full-access`

Use the least privilege compatible with the task.

### `skip_git_repo_check`

Useful when the target is not a Git repository.

### `ignore_user_config`

Passes Codex's `--ignore-user-config`.

Normally leave false.

### `ignore_rules`

Passes Codex's `--ignore-rules`.

Normally leave false; project rules may be important.

---

## `[paths]`

Paths are resolved relative to the directory containing `config.ini`.

### `project_root`

The target directory given to Codex through `-C`.

Default `..` assumes the harness lives directly inside the target project.

### `task_file`

Default `task.md`.

### `prometheus_file`

Default `Prometheus.md`.

### `momus_file`

Default `Momus.MD`.

### `schema_file`

Default `schema.json`.

### `runs_dir`

Completed run archives.

---

## `[output]`

### `publish_prompts`

Copies every exact controller prompt into the run archive.

Useful for reproducibility.

### `publish_raw_jsonl`

Copies raw `codex exec --json` streams into the run archive.

### `keep_private_runtime_on_success`

Usually false because the final run has already been archived.

### `keep_private_runtime_on_failure`

Usually true for debugging failed/interrupted runs.
