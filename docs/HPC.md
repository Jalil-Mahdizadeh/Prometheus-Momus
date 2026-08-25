# HPC / Slurm notes

Prometheus–Momus can be run from an interactive or batch Slurm allocation.

## Architecture compatibility

The local Codex CLI binary must match the compute node architecture.

For example:

```bash
uname -m
file "$(which codex)"
codex --version
```

An ARM64 Codex binary should be run on an ARM64 node.

## Allocation lifetime

The Slurm allocation walltime and `budget.max_wall_minutes` are independent.
Configure the controller budget below the scheduler limit so it can archive a
clean budget-exhausted outcome before Slurm sends termination signals. Leave
at least one heartbeat interval of margin.

Check:

```bash
echo "$SLURM_JOB_ID"
squeue -j "$SLURM_JOB_ID"
```

`tmux`, `screen`, and `nohup` protect against terminal disconnects, but they
cannot keep the debate alive after Slurm terminates the allocation.

## Low CPU is normal

Codex model inference is remote. The local process may spend substantial time
waiting on remote inference, web search, or tool calls.

A low CPU percentage does not necessarily indicate a stalled turn.

## Monitor processes

```bash
pgrep -af 'codex|debate.py'
```

and:

```bash
ps -o pid,ppid,etime,%cpu,%mem,stat,cmd \
  -p $(pgrep -d, -f 'codex|debate.py')
```

## Monitor the debate

The controller prints the durable live transcript path.

Use another shell:

```bash
tail -f .prometheus-momus-state/<run-id>/DEBATE_TRANSCRIPT.md
```

## Batch jobs

For long unattended debates, a Slurm batch allocation is often preferable to
an interactive session. Ensure the node has outbound connectivity required by
your Codex setup and request enough walltime for multiple high-reasoning
turns.

## Scheduler interruption and resume

A completed protocol transition survives allocation loss:

```bash
python3 debate.py --resume <run-id>
```

The controller converts `SIGTERM` into a graceful interruption, terminates
the active Codex process group, and preserves the already-written in-flight
checkpoint. Scheduler-level `SIGKILL` cannot run cleanup, but the pre-call
checkpoint still remains.

If Slurm terminated the job during a Codex call, the checkpoint remains
`inflight`. Inspect the last prompt/raw JSONL and acknowledge possible
persistent-thread replay explicitly:

```bash
python3 debate.py --resume <run-id> --retry-inflight
```

Do not edit the task, roles, schemas, or config between the original run and
resume; their hashes are part of the checkpoint contract.

Linux compute nodes must permit unprivileged user namespaces for bubblewrap.
`./check.sh` exercises actual namespace creation inside the allocation.
