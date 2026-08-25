# Security

Prometheus–Momus orchestrates autonomous model sessions. Treat the target
repository, model output, connected tools, and web content as untrusted input.

## Enforced blind boundary

With `blind_second_agent = true`, startup requires
`isolation.enabled = true` and fails closed if the configured backend cannot
create its boundary. Blind mode also requires `sandbox = read-only`; this
prevents Prometheus from leaking its opening through the shared project.

On Linux, bubblewrap gives each agent:

- a separate mount, user, PID, IPC, UTS, and cgroup namespace;
- the project mounted at `/workspace`;
- only that agent's output directory and Codex home;
- controller state hidden by a private mount;
- a cleared environment with a small proxy/certificate allowlist.

`/usr`, `/etc`, and an existing Nix store are mounted read-only for the
runtime; `/opt`, home-directory contents, and arbitrary host paths are not.

On macOS, `sandbox-exec` can apply a deny-by-default filesystem profile and
a separate Codex home for non-blind runs. It is not accepted as the enforced
blind boundary: Apple DTS describes SBPL as undocumented for third-party use.
See the [Apple Developer Forums guidance](https://developer.apple.com/forums/thread/661939).

Prometheus, Momus, and an optional model adjudicator use distinct Codex homes.
Live status does not publish transcript paths or thread identifiers. The
preflight performs a real namespace/profile creation test.

## Boundary limitations

This is an OS process boundary, not a VM or cryptographic proof. It does not
defend against:

- kernel, bubblewrap, optional Seatbelt, Codex CLI, or model-provider
  compromise;
- information already present in the target project or prior published runs;
- collusion through external services or accounts deliberately exposed to both
  agents;
- correlated reasoning errors from similar models;
- denial of service by an agent;
- prompt injection inside project or web content.

The Codex CLI must access its authentication material. Do not assume the model
tool sandbox protects credentials from a malicious local executable. Use a
dedicated, least-privilege Codex account for hostile projects.

## Extra read paths

`isolation.extra_read_paths` should contain only narrow, required paths. The
controller rejects root/home paths and any path that overlaps controller
state. Exposing broad directories weakens the boundary.

## Target scope

The bundled `project_root = .` is intentionally narrow. Prefer an explicit
`--project-root` for external targets, and never point it at a home directory,
filesystem root, or broad workspace containing unrelated private data. The
target is visible to both agents and is also the trust boundary for
project-file evidence.

## Codex sandbox

The outer isolation boundary controls which host data exists in the agent's
view. The Codex `sandbox` setting independently controls what model-generated
tools may modify.

Keep:

```ini
sandbox = read-only
```

unless the task genuinely requires writes. For `workspace-write`, use a
version-controlled or disposable target and disable blind mode; the controller
rejects write-capable blind runs.

The controller also rejects write-capable runs when its code, config, task,
roles, or schemas are inside `project_root`. Store the harness outside the
writable target so an agent cannot rewrite the program later used to resume.

## Evidence and adjudication

Agent `ACCEPT` is tentative. The controller rejects acceptance with blocking
issues, missing required evidence, or disputed evidence. Project files are
resolved inside the project and hashed; URLs are syntax-checked. These checks
prove provenance/integrity, not the truth of the associated claim.

Consensus is published only after an independent human or explicitly different
model verifies every evidence source. This reduces correlated blind spots but
does not replace empirical validation, expert review, or legal/safety review.

## Durable state and archives

Private checkpoints live under `.prometheus-momus-state/<run-id>/` and are
excluded from Git. Interrupted calls remain marked in-flight; replay requires
`--retry-inflight` because the persistent thread may already have advanced.

Terminal archives can contain prompts, model responses, project observations,
source URLs, and evidence excerpts. Review them before sharing. Never place
passwords, API keys, or unrelated private data in task/role files or exposed
project paths.
