<p align="center">
  <img src="assets/Prometheus-Monus.png"
       alt="Prometheus–Momus"
       width="100%">
</p>

# Prometheus–Momus

**Prometheus–Momus** is a small, auditable harness for running two
persistent OpenAI Codex CLI agents in an autonomous adversarial loop.

- **Prometheus** constructs and synthesizes.
- **Momus** independently analyzes, challenges, and counterproposes.
- A Python controller manages persistent threads, turn-taking, structured
  output, mandatory challenge rounds, enforced agent isolation, optional hard
  budgets,
  durable checkpoints, evidence ledgers, independent adjudication, and
  crash-atomic run archives.

The package is deliberately **domain-neutral**. It can be used for research
design, architecture reviews, repository strategy, experimental planning,
technical decisions, product design, or other tasks where independent
proposal/challenge cycles are useful.

> This is an **unofficial community tool**. It is not an OpenAI product and is
> not affiliated with or endorsed by OpenAI.

## Why two persistent agents?

Each agent keeps its own Codex conversation history:

```text
Prometheus thread A                  Momus thread B
       |                                  |
 independent opening                 independent blind analysis
       |                                  |
       +---------- candidate ------------>|
                                          |
                                     challenge / counter
                                          |
       |<------------- counter -----------+
       |
  challenge / counter
       |
       +----------------------------------> ...
                                          |
                               tentative ACCEPT
                                          |
                               independent gate
                                          |
                                  APPROVE / REJECT
```

The controller exposes the opponent's **current candidate**, not the other
agent's entire conversation. With the default settings, Momus's initial
analysis is isolated from Prometheus's response by a separate filesystem view
and Codex home; Linux additionally uses separate PID/user namespaces.

## What users edit

The task and optional role customization are concentrated in three files:

1. `task.md` — complete this neutral template with the actual problem and
   success criteria.
2. `Prometheus.md` — reusable constructive-agent role; edit only if needed.
3. `Momus.md` — reusable adversarial-agent role; edit only if needed.

All runtime parameters are explicitly populated in:

4. `config.ini` — rounds, model, isolation, budgets, adjudication, and output.

Normally users only complete `task.md`, select the target, and adjust values in
`config.ini`; the controller and JSON schemas require no changes.

---

## Requirements

- Linux for enforced blind mode; macOS only for explicitly non-blind runs
- Python 3.9+
- OpenAI Codex CLI installed and authenticated
- Linux: `bubblewrap` (`bwrap`); optional macOS containment:
  `sandbox-exec`
- A Codex CLI version supporting:
  - `codex exec`
  - `codex exec resume`
  - `--json`
  - `--output-schema`
  - `--output-last-message`
  - `--sandbox`

Codex CLI changes over time, so run the included preflight check after
installing or upgrading Codex.

## Quick start

### Option A — keep the harness separate

Complete the bundled task template, then point both preflight and execution at
the intended target:

```bash
cd /path/to/prometheus-momus

${EDITOR:-vi} task.md
${EDITOR:-vi} Prometheus.md
${EDITOR:-vi} Momus.md

./check.sh --project-root /path/to/target
./run.sh --project-root /path/to/target
```

The safe standalone default is `project_root = .`. Passing
`--project-root` avoids accidentally exposing a broader parent directory and
does not modify `config.ini`.

### Option B — place the harness inside a project

An embedded layout can use:

```text
my-project/
├── source/
├── data/
└── .codex-debate/
    ├── debate.py
    ├── config.ini
    ├── task.md
    ├── Prometheus.md
    ├── Momus.md
    ├── schema.json
    ├── adjudication_schema.json
    ├── runtime_isolation.py
    ├── controller_safety.py
    ├── run.sh
    └── check.sh
```

Copy or rename this package folder to `.codex-debate`, complete the semantic
files, and set `project_root = ..` in its `config.ini`. Then:

```bash
cd /path/to/my-project

.codex-debate/check.sh
.codex-debate/run.sh
```

## Configuration

Edit `config.ini`.

The bundled profile is domain-neutral, configured for a large autonomous
debate, and requires no human adjudication:

```ini
[debate]
min_counter_rounds = 4
max_counter_rounds = 10
blind_second_agent = true
final_acceptance_audit = true

[codex]
model = gpt-5.6-sol
reasoning_effort = max
web_search = live
sandbox = read-only

[isolation]
enabled = true
backend = auto

[budget]
max_model_calls = 0
max_wall_minutes = 0
max_total_tokens = 0
max_estimated_cost_usd = 0

[adjudication]
mode = model
model = gpt-5.5
reasoning_effort = xhigh
```

For the four budget ceilings, `0` means unlimited. This avoids terminating a
large debate because of a generic package limit, but it can incur substantial
usage and cost. Set positive ceilings before running when bounded spend or
runtime matters. The populated price rates make cost enforcement available by
changing only `max_estimated_cost_usd`; verify current pricing first.

See `docs/CONFIGURATION.md` for all options.

## Read-only by default

The default is:

```ini
sandbox = read-only
```

That means the Codex agents can inspect the project but cannot modify it.
The Python controller itself can still write its own run archives.

For brainstorming, review, research, and design debates, read-only is the
recommended mode.

Only switch to `workspace-write` if the debate task genuinely requires the
agents to modify the target project and you understand the implications.
Write-capable modes require `blind_second_agent = false`.
They also require the harness and all semantic/config/schema files to live
outside `project_root`, preventing an agent from rewriting its controller.

## Persistent sessions

The first call for each agent creates a Codex thread. Subsequent turns use:

```text
codex exec resume <thread_id>
```

so Prometheus and Momus retain separate histories.

Do **not** use Codex's ephemeral mode with this harness; persistent sessions
are the core of the design.

## Structured output

Every turn is constrained by `schema.json`:

```json
{
  "decision": "PROPOSE | COUNTER | ACCEPT",
  "critique": [],
  "proposal": "...complete standalone state...",
  "blocking_issues": [],
  "rationale": "...",
  "evidence": [
    {
      "claim": "...",
      "source": "...",
      "source_type": "project_file | url | calculation | experiment | other",
      "status": "verified | unverified | disputed",
      "notes": "..."
    }
  ]
}
```

This avoids fragile prose parsing.

## Acceptance guards

The controller provides several protocol safeguards:

- minimum mandatory counter-rounds;
- premature-acceptance repair;
- rejection of `ACCEPT` with blocking issues;
- rejection of `ACCEPT` with missing or disputed evidence;
- configurable final acceptance audit;
- hard maximum counter-round limit;
- independent human or heterogeneous-model adjudication before consensus.

If the agents do not agree within the limit, the run ends as
`NO_CONSENSUS`, not forced consensus.

## Blind Momus pre-analysis

When enabled, Momus performs its initial analysis without being shown
Prometheus's opening response.

Each agent receives only the project, its own private Codex home, its own
output directory, required system files, and explicitly configured extra read
paths. The controller state directory is hidden even when it sits inside the
project. Startup fails closed when blind mode is requested but this boundary
cannot be created.

This materially enforces blindness, but it is still not a VM or a defense
against kernel, Codex service, or model-provider compromise. See
`SECURITY.md` for the exact boundary.

## Monitoring long turns

The controller prints a heartbeat:

```text
[Prometheus] still running — elapsed 03:00
```

It also prints the path to a private live transcript. From another terminal:

```bash
tail -f .prometheus-momus-state/<run-id>/DEBATE_TRANSCRIPT.md
```

Package-level status is available in:

```text
RUN_STATUS.json
```

## Run archives

Every run receives a unique ID and is archived automatically:

```text
runs/
└── 20260824-225500-2f93a1cd/
    ├── CONSENSUS.md             # or REJECTED/NO_CONSENSUS/BUDGET_EXHAUSTED
    ├── ADJUDICATION.json        # when adjudication occurred
    ├── DEBATE_TRANSCRIPT.md
    ├── history.jsonl
    ├── final_candidate.json
    ├── run_manifest.json
    ├── task.snapshot.md
    ├── Prometheus.snapshot.md
    ├── Momus.snapshot.md
    ├── config.snapshot.ini
    ├── schema.snapshot.json
    ├── evidence_audit.jsonl
    ├── checkpoint.json
    ├── responses/
    ├── prompts/                 # configurable
    └── raw-jsonl/               # configurable
```

`LATEST_RUN.txt` points to the most recently archived run.

No manual cleanup is required before starting another debate.

## Durable resume

The controller checkpoints every committed protocol transition and records a
model call as in-flight before launching it:

```bash
python3 debate.py --resume <run-id>
```

If a process died during a model call, automatic replay is refused because
the persistent session may already have advanced. After reviewing that risk:

```bash
python3 debate.py --resume <run-id> --retry-inflight
```

Input hashes and effective project/control paths must still match the original
task, roles, schemas, and config.

## Independent adjudication

The default `model` mode runs an isolated `gpt-5.5` adjudicator at `xhigh`
effort after tentative agreement, so the normal workflow is unattended. The
adjudicator is separate from both debate sessions and must verify every final
evidence source before approval.

To require a person instead, set `mode = human`. Agent agreement will then
create a review packet without publishing consensus. Complete the generated
JSON template, independently verify every evidence source, and run:

```bash
python3 debate.py --adjudicate <run-id> --review-file review.json \
  --reviewer "name-or-auditable-id"
```

Any model adjudicator must use an explicitly different model from both
debating agents.

## Preflight

Run:

```bash
./check.sh
```

or:

```bash
python3 debate.py --check
```

The check verifies local files, Python, Codex CLI flags, schemas, effective
settings, and actual namespace/profile creation. It does **not** make a model
call.

## Useful CLI options

```bash
python3 debate.py --check
python3 debate.py --print-config
python3 debate.py --project-root /path/to/project
python3 debate.py --config /path/to/config.ini
python3 debate.py --resume <run-id>
python3 debate.py --resume <run-id> --retry-inflight
python3 debate.py --adjudicate <run-id> --review-file review.json \
  --reviewer "name-or-auditable-id"
python3 debate.py --version
```

## HPC use

The harness works well inside a Slurm interactive or batch allocation. Codex
inference is remote, so local CPU utilization can be low even while a turn is
active.

Important: `tmux`, `screen`, or `nohup` cannot preserve a process after the
Slurm allocation itself expires.

See `docs/HPC.md`.

For CLI compatibility notes, see `docs/CODEX_COMPATIBILITY.md`.

## Security and trust

Prompts, project files, connected tools, and web content can influence agent
behavior.

- Use `read-only` unless writes are necessary.
- Keep outer isolation enabled for blind runs.
- Treat untrusted repositories as untrusted input.
- Review external commands before enabling write-capable modes.
- Treat agent agreement as tentative until independent adjudication.
- Agent consensus does not replace domain-expert, legal, medical, financial,
  security, or safety review where those are required.

See `SECURITY.md`.

## Design philosophy

The harness deliberately does **not** encode a particular domain methodology.
Put domain-specific sequencing, evaluation criteria, stop conditions, required
external research, or evidence standards in `task.md` and, only when needed,
the two role files.

The controller should orchestrate the debate, not decide the answer.

## License

MIT. See `LICENSE`.
