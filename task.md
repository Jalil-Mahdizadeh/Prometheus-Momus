# Task Specification Template

This file is intentionally domain-neutral. Replace the instructional
placeholders before starting a consequential debate. Be concrete: the agents
can only evaluate criteria, evidence, and constraints that the task states or
that they can legitimately inspect.

## Objective

[State the exact question, decision, problem, or outcome the debate must
resolve.]

## Context

[Describe the current situation, why the decision matters, and any relevant
history. Identify the project files, systems, users, or stakeholders that
define the problem.]

## Required deliverable

[Specify the form of the final work product: recommendation, design, plan,
review, root-cause analysis, prioritized options, or another concrete output.]

## Scope

### In scope

- [Item the agents must address.]
- [Item the agents must address.]

### Out of scope

- [Explicit exclusion.]
- [Explicit exclusion.]

## Hard constraints

- [Budget, deadline, compatibility, policy, safety, or resource constraint.]
- [Required interface, dependency, platform, or operating condition.]
- [Action the agents must not recommend or take.]

## Required inputs

- [Project-relative file or directory the agents must inspect.]
- [Canonical external source, dataset, specification, or other input.]
- [Known fact or decision that should be treated as authoritative.]

If current external facts matter, require live verification and identify the
preferred source types. If web research is unnecessary, say so explicitly.

## Evaluation criteria

Rank or weight the criteria that determine the best candidate. Adapt this list
to the task:

1. Correctness and requirement coverage.
2. Evidence quality and reproducibility.
3. Feasibility under the stated constraints.
4. Safety, security, privacy, and reliability.
5. Cost, performance, and operational burden.
6. Maintainability and reversibility.
7. Simplicity relative to delivered value.

State any mandatory threshold that overrides the ranking.

## Alternatives to consider

Require comparison with:

- the status quo or no-action option;
- the simplest viable approach;
- the strongest credible alternative;
- any named candidate that must be included or excluded.

## Evidence standard

For each claim that could change the decision, require an evidence-ledger
entry. Use project-relative paths for repository evidence and canonical URLs
for external evidence. Distinguish facts, calculations, assumptions, and
inferences. Mark unresolved or disputed claims honestly.

## Risk and validation

Require the agents to identify:

- the strongest argument against the preferred candidate;
- likely failure and misuse modes;
- security, privacy, compliance, and operational risks where relevant;
- a proportionate test or review that could falsify the recommendation;
- rollback, recovery, or exit conditions;
- unknowns that require a human decision.

## Required final structure

The complete proposal should contain:

### A. Recommendation

A direct answer and decision status appropriate to the task.

### B. Assumptions and constraints

The material conditions on which the answer depends.

### C. Options considered

A concise comparison of credible alternatives, including the status quo and
the simplest viable option.

### D. Selected proposal

The complete design, plan, or recommendation at an implementation-ready level
appropriate to the task.

### E. Evidence

The decisive supporting and contradicting evidence, with uncertainty stated.

### F. Risks and mitigations

Material failure modes, safeguards, tradeoffs, and residual risk.

### G. Execution plan

Sequenced actions, owners or decision points, dependencies, and validation
steps.

### H. Success and stop criteria

Observable measures for success, plus results or conditions that should cause
revision, rollback, or rejection.

### I. Unresolved questions

Only questions that materially prevent or qualify the decision.

## Acceptance standard

Recommend acceptance only when the candidate:

- answers the stated objective directly;
- satisfies every hard constraint;
- is supported by a non-disputed evidence ledger;
- compares credible alternatives fairly;
- includes a feasible validation and execution path;
- exposes material uncertainty and residual risk; and
- has no unresolved blocking issue.

A negative or no-action recommendation is valid when it is the most defensible
outcome. Do not manufacture agreement or novelty merely to avoid it.
