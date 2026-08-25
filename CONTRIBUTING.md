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
- portable examples with no local paths or domain-specific bundled task;
- standard-library-only Python where practical.

## Before submitting changes

Run:

```bash
python3 -m py_compile debate.py controller_safety.py runtime_isolation.py
python3 -m unittest discover -s tests -v
python3 debate.py --check
```

The preflight check does not make a model call.

The real Codex compatibility smoke test is deliberately opt-in:

```bash
PROMETHEUS_MOMUS_REAL_CODEX_SMOKE=1 \
  python3 -m unittest tests.test_real_codex_smoke -v
```

It spends two model calls. Do not enable it in ordinary unit-test jobs.

If modifying the Codex invocation, verify the behavior against the current
official Codex CLI because flags and configuration options can evolve.
