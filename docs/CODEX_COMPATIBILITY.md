# Codex CLI compatibility

Prometheus–Momus uses public non-interactive Codex CLI primitives rather than
terminal scraping.

The core invocation relies on:

- `codex exec`
- `codex exec resume <thread_id>`
- `--json`
- `--output-schema`
- `--output-last-message`
- `--sandbox`
- `-C` / `--cd`
- optionally `--skip-git-repo-check`
- optionally `--model`
- optionally Codex `--config` overrides for reasoning effort and web search.

As of 25 August 2026 these primitives are present in the official
`openai/codex` source:

- CLI exec definition:
  https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs
- resume/output-schema tests:
  https://github.com/openai/codex/blob/main/codex-rs/exec/tests/suite/resume.rs
- TypeScript exec wrapper showing model, sandbox, working-directory,
  skip-git-check, output-schema, reasoning-effort, and web-search arguments:
  https://github.com/openai/codex/blob/main/sdk/typescript/src/exec.ts

Codex evolves rapidly. The package therefore includes:

```bash
./check.sh
```

which checks for the CLI and expected flags without making a model call.

The controller also relies on terminal JSONL usage events (currently
`turn.completed.usage`) when token/cost ceilings are enabled. Missing usage
fails closed rather than silently disabling accounting.

## Output-file defensive behavior

The harness creates all output parent directories before invoking
`--output-last-message`.

This is deliberate because some Codex CLI releases have reported late
output-file failures when a parent directory does not already exist.

## Resume behavior

The harness repeats model, sandbox, schema, web-search, and related CLI
options on every resumed call. Do not assume all invocation flags from the
original call are automatically the intended settings for later turns.

Round-limit continuation uses the same `codex exec resume <thread-id>`
primitive and, for isolated runs, retained per-agent Codex homes.
`--extra-rounds` introduces no additional Codex CLI requirement.

The test suite includes a two-call real CLI smoke test for schema-constrained
output across resume. It is opt-in because it spends model calls:

```bash
PROMETHEUS_MOMUS_REAL_CODEX_SMOKE=1 \
  python3 -m unittest tests.test_real_codex_smoke -v
```

Set `PROMETHEUS_MOMUS_SMOKE_MODEL` to pin a test model.

## Outer isolation compatibility

Linux isolation binds the resolved Codex executable at `/codex`. When the
installed bundle includes the adjacent `codex-code-mode-host` companion, it is
also mounted at `/codex-code-mode-host`; current Codex releases may launch it
for code-mode tools. Dynamically linked or wrapper-based installations may
require their narrow runtime paths in `isolation.extra_read_paths`. Preflight
catches namespace creation problems, while the opt-in real smoke test catches
end-to-end invocation changes.

## Version policy

The package intentionally does not pin a Codex CLI version.

Users should:

1. install a current Codex CLI;
2. authenticate normally;
3. run `./check.sh`;
4. test their chosen model/reasoning/web-search settings before a costly
   multi-round debate.
