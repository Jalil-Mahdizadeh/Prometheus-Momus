# Workflow design guide

The harness is intentionally domain-neutral. The quality of the debate depends
heavily on the three semantic files.

## `task.md`

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
leakage controls, statistics, and falsification when relevant.

For software architecture, explicitly require repository inspection, tests,
operational constraints, migration risk, and simpler alternatives.

## `Prometheus.md`

Prometheus should usually be constructive and synthesis-oriented.

Useful role traits:

- originality;
- implementation focus;
- systems thinking;
- willingness to replace its own weak ideas;
- requirement for complete standalone counterproposals.

## `Momus.MD`

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

## Final acceptance audit

Keeping this enabled is recommended. It turns a tentative ACCEPT into one last
falsification attempt rather than immediately terminating.
