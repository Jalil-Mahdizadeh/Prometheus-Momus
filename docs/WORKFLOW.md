# Workflow design guide

The controller and the three bundled semantic templates are domain-neutral.
Debate quality depends heavily on how those files are completed.

## `task.md`

Replace the bracketed guidance with the actual problem before a consequential
run.

A strong task usually specifies:

- exact goal;
- background/context;
- constraints;
- information the agents must inspect;
- evaluation criteria;
- required evidence;
- success criteria;
- kill/rejection criteria;
- required final output.

For research tasks, explicitly require literature search, source quality,
leakage controls, statistics, and falsification when relevant. For software
changes, require repository inspection, tests, operational constraints,
migration/rollback risk, and simpler alternatives. Apply equivalent
domain-specific standards to other tasks.

The structured evidence ledger should contain only claims that materially
affect the candidate. Use stable project-relative paths or canonical URLs,
state uncertainty honestly, and include enough notes for a later reviewer to
reproduce the check.

## `Prometheus.md`

Prometheus should usually be constructive and synthesis-oriented.

Useful role traits:

- task and requirement fidelity;
- evidence-aware reasoning;
- implementation focus;
- systems thinking;
- willingness to replace its own weak ideas;
- requirement for complete standalone counterproposals.

## `Momus.md`

Momus should usually be adversarial without becoming contrarian for its own
sake.

Useful role traits:

- correctness review;
- hidden-assumption search;
- simpler-alternative search;
- prior-art search;
- scalability review;
- failure-mode analysis;
- kill criteria.

## Minimum rounds

More rounds do not automatically mean a better answer.

Typical starting points:

- routine technical decision: 2–3 minimum, 5–6 maximum;
- complex research design: 3–4 minimum, 8 maximum;
- very high-value adversarial review: 4 minimum, 8–10 maximum.

High-reasoning models can make each turn expensive.

Set call, wall-time, and token budgets before increasing round counts. Repairs
and the final audit also consume calls.

The bundled 4–10 round range targets large adversarial work. Lower it for
routine tasks. Its model-call, wall-time, token, and cost ceilings are
unlimited by default; set positive limits whenever bounded usage matters.

If a useful debate reaches its ceiling with unresolved material issues, extend
that same debate instead of editing `max_counter_rounds` or starting over:

```bash
python3 debate.py --resume <run-id> --extra-rounds 3
```

Extensions preserve both agents' histories and can be repeated. Add rounds
deliberately: accumulated usage and configured budgets carry forward, and more
rounds are valuable only when they can resolve a concrete remaining issue.

## Final acceptance audit

Keeping this enabled is recommended. It turns a tentative ACCEPT into one last
falsification attempt rather than immediately terminating.

## Adjudication

The bundled `none` mode archives agent agreement as explicitly unadjudicated
consensus. Use `model` when an independent, heterogeneous-model gate is
valuable, and use `human` whenever the decision requires accountable expert
approval. Model adjudication does not replace legally or operationally
required human review.
