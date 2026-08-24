# Architecture

## Components

```text
                   debate.py
                       |
          +------------+------------+
          |                         |
          v                         v
     Prometheus                  Momus
   persistent thread          persistent thread
          |                         |
    independent opening       blind pre-analysis
          |                         |
          +-------- candidate ----->|
                                    |
                               COUNTER
                                    |
          |<------------------------+
          |
        COUNTER
          |
          +------------------------> ...
                                    |
                                  ACCEPT
                                    |
                          final acceptance audit
                                    |
                         +----------+----------+
                         |                     |
                    CONSENSUS            COUNTER again
```

## Context isolation

Each Codex agent has its own persistent thread.

The opponent's full conversation is never inserted into the other thread.
The controller injects only the current standalone candidate.

Before Momus completes its initial independent analysis, Prometheus's response
is stored under a temporary runtime directory outside the project workspace.

During the entire live debate, detailed transcripts, prompts, and raw model
events remain private to the controller. The final audit package is copied
into `runs/<run-id>/` only when the run ends or fails.

This reduces accidental cross-agent contamination.

It is not a cryptographic isolation mechanism because Codex itself maintains
session infrastructure outside this harness.

## Controller versus model permissions

The Codex sandbox applies to the agent process.

For example, with:

```ini
sandbox = read-only
```

Codex can inspect the project but cannot modify it.

The controller is a normal Python process running as the user and can write
its own logs/run archives.

## Persistent threads

A new agent call uses:

```text
codex exec ... -
```

The emitted `thread_id` is retained.

Later calls use:

```text
codex exec ... resume <thread_id> -
```

CLI options are supplied again on resumed calls.

## Output schema

The controller never attempts to infer ACCEPT/COUTER from free-form prose.
Codex is instructed to satisfy `schema.json`, and the Python controller
validates the parsed object.

## Failure behavior

The harness archives partial work on exceptions and interruptions.

A run is never silently converted to consensus when the configured round
limit is reached.
