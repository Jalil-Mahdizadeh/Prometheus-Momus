# Prometheus–Momus

**Prometheus–Momus** is a small, auditable Linux/macOS harness for running two
persistent OpenAI Codex CLI agents in an autonomous adversarial loop.

- **Prometheus** constructs and synthesizes.
- **Momus** independently analyzes, challenges, and counterproposes.
- A Python controller manages persistent threads, turn-taking, structured
  output, mandatory challenge rounds, acceptance guards, and run archives.

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
                                      ACCEPT
```

The controller exposes the opponent's **current candidate**, not the other
agent's entire conversation. Momus's initial analysis is operationally blind
to Prometheus's opening response.

## What users edit

The semantic behavior is intentionally concentrated in three files:

1. `task.md` — the actual problem and success criteria.
2. `Prometheus.md` — constructive-agent role.
3. `Momus.md` — adversarial-agent role.

Runtime behavior lives in:

4. `config.ini` — rounds, model override, reasoning effort, web search,
   sandbox, heartbeat, timeout, and output settings.

Normally you should not need to edit `debate.py` or `schema.json`.

---

## Requirements

- Linux or macOS
- Python 3.9+
- OpenAI Codex CLI installed and authenticated
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

### Option A — place the harness inside a project

A convenient layout is:

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
    ├── run.sh
    └── check.sh
```

Rename or copy this package folder to `.codex-debate` inside the target
project.

Then:

```bash
cd /path/to/my-project

nano .codex-debate/task.md
nano .codex-debate/Prometheus.md
nano .codex-debate/Momus.md

.codex-debate/check.sh
.codex-debate/run.sh
```

The default `config.ini` uses `project_root = ..`, so Codex works on the
parent project.

### Option B — keep the harness elsewhere

Pass the target project explicitly:

```bash
python3 /path/to/prometheus-momus/debate.py \
  --project-root /path/to/my-project
```

## Configuration

Edit `config.ini`.

Typical research-heavy setup:

```ini
[debate]
min_counter_rounds = 4
max_counter_rounds = 8
blind_second_agent = true
final_acceptance_audit = true

[codex]
model = gpt-5.6-sol
reasoning_effort = max
web_search = live
sandbox = read-only
```

For maximum portability, the distributed defaults leave `model` and
`reasoning_effort` blank, which means **inherit the user's Codex config**.

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
  "rationale": "..."
}
```

This avoids fragile prose parsing.

## Acceptance guards

The controller provides several protocol safeguards:

- minimum mandatory counter-rounds;
- premature-acceptance repair;
- rejection of `ACCEPT` with blocking issues;
- optional mandatory final acceptance audit;
- hard maximum counter-round limit.

If the agents do not agree within the limit, the run ends as
`NO_CONSENSUS`, not forced consensus.

## Blind Momus pre-analysis

When enabled, Momus performs its initial analysis without being shown
Prometheus's opening response.

Live debate content is stored under a private temporary directory rather
than in the project during the run. This prevents ordinary project
inspection from revealing Prometheus's hidden answer.

This is **operational isolation, not a cryptographic security boundary**.
Codex session infrastructure exists outside the harness, so the role prompt
also explicitly forbids inspecting rollout/session/orchestration artifacts
to defeat blindness.

## Monitoring long turns

The controller prints a heartbeat:

```text
[Prometheus] still running — elapsed 03:00
```

It also prints the path to a private live transcript. From another terminal:

```bash
tail -f /tmp/prometheus-momus-.../DEBATE_TRANSCRIPT.md
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
    ├── CONSENSUS.md             # or NO_CONSENSUS.md
    ├── DEBATE_TRANSCRIPT.md
    ├── history.jsonl
    ├── final_candidate.json
    ├── run_manifest.json
    ├── task.snapshot.md
    ├── Prometheus.snapshot.md
    ├── Momus.snapshot.md
    ├── config.snapshot.ini
    ├── schema.snapshot.json
    ├── responses/
    ├── prompts/                 # configurable
    └── raw-jsonl/               # configurable
```

`LATEST_RUN.txt` points to the most recently archived run.

No manual cleanup is required before starting another debate.

## Preflight

Run:

```bash
./check.sh
```

or:

```bash
python3 debate.py --check
```

The check verifies local files, Python, Codex availability, expected CLI
flags, and effective settings. It does **not** make a model call.

## Useful CLI options

```bash
python3 debate.py --check
python3 debate.py --print-config
python3 debate.py --project-root /path/to/project
python3 debate.py --config /path/to/config.ini
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
- Treat untrusted repositories as untrusted input.
- Review external commands before enabling write-capable modes.
- Consensus does not imply correctness.
- Literature/patent consensus does not constitute legal advice.

See `SECURITY.md`.

## Design philosophy

The harness deliberately does **not** encode a particular scientific
methodology. Put domain-specific sequencing, evaluation criteria, kill
criteria, required web research, or evidence standards in `task.md` and the
two role files.

The controller should orchestrate the debate, not decide the answer.

## License

MIT. See `LICENSE`.
