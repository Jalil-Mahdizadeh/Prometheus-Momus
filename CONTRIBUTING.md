# Contributing

Contributions are welcome.

## Principles

Changes should preserve:

- domain neutrality;
- explicit, auditable orchestration;
- separate persistent agent histories;
- no forced consensus;
- least-privilege defaults;
- no silent loss of run artifacts;
- standard-library-only Python where practical.

## Before submitting changes

Run:

```bash
python3 -m py_compile debate.py
python3 debate.py --check
```

The preflight check does not make a model call.

If modifying the Codex invocation, verify the behavior against the current
official Codex CLI because flags and configuration options can evolve.
