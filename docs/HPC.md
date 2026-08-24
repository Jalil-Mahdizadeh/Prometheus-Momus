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

The critical constraint is the Slurm allocation walltime.

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

The controller prints the temporary live transcript path.

Use another shell:

```bash
tail -f /tmp/prometheus-momus-.../DEBATE_TRANSCRIPT.md
```

## Batch jobs

For long unattended debates, a Slurm batch allocation is often preferable to
an interactive session. Ensure the node has outbound connectivity required by
your Codex setup and request enough walltime for multiple high-reasoning
turns.
