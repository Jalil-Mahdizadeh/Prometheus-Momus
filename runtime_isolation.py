#!/usr/bin/env python3
"""Fail-closed filesystem isolation for Prometheus-Momus Codex agents.

The Codex sandbox controls model-generated tool permissions.  This module adds
an outer OS boundary whose job is narrower: an agent may see the project and
its own Codex/session directory, but never the controller runtime or another
agent's session directory.

Linux uses bubblewrap mount/PID/user namespaces.  macOS uses a deny-by-default
Seatbelt profile through ``sandbox-exec`` when that command is available.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional


class IsolationError(RuntimeError):
    """Raised when a requested isolation boundary cannot be enforced."""


@dataclass(frozen=True)
class ExecutionPaths:
    codex: str
    project_root: str
    schema_file: str
    output_file: str


def _resolved_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _safe_agent_name(name: str) -> str:
    if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in name):
        raise IsolationError(f"Unsafe agent name: {name!r}")
    return name


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    result = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return tuple(result)


class IsolationManager:
    """Construct an OS-enforced execution boundary for each Codex agent."""

    SYSTEM_READ_PATHS = (
        Path("/usr"),
        Path("/etc"),
        Path("/nix"),
    )
    SYSTEM_LINK_PATHS = (
        Path("/bin"),
        Path("/sbin"),
        Path("/lib"),
        Path("/lib64"),
    )
    CODEX_RESOURCE_DIRS = ("rules", "skills", "plugins")
    FORWARDED_ENV = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NIX_SSL_CERT_FILE",
    )

    def __init__(
        self,
        *,
        enabled: bool,
        backend: str,
        project_root: Path,
        controller_state_root: Path,
        run_private_dir: Path,
        extra_read_paths: Iterable[Path] = (),
        codex_executable: Optional[Path] = None,
        codex_home: Optional[Path] = None,
    ) -> None:
        self.enabled = enabled
        self.project_root = project_root.resolve()
        self.controller_state_root = controller_state_root.resolve()
        self.run_private_dir = run_private_dir.resolve()
        self.extra_read_paths = _unique_paths(extra_read_paths)
        self.codex_home = (codex_home or _resolved_codex_home()).resolve()

        discovered_codex = shutil.which("codex")
        executable = codex_executable or (
            Path(discovered_codex) if discovered_codex else None
        )
        self.codex_executable = executable.resolve() if executable else None
        self.backend = self._resolve_backend(backend) if enabled else "none"

        self._validate_paths()

    def _resolve_backend(self, requested: str) -> str:
        requested = requested.strip().lower()
        if requested not in {"auto", "bubblewrap", "sandbox-exec"}:
            raise IsolationError(
                "isolation backend must be auto, bubblewrap, or sandbox-exec"
            )

        system = platform.system()
        if requested == "auto":
            if system == "Linux" and shutil.which("bwrap"):
                return "bubblewrap"
            if system == "Darwin" and shutil.which("sandbox-exec"):
                return "sandbox-exec"
            raise IsolationError(
                "No supported fail-closed isolation backend is available "
                "(Linux requires bwrap; macOS requires sandbox-exec)."
            )

        if requested == "bubblewrap" and not shutil.which("bwrap"):
            raise IsolationError("bubblewrap isolation requested but bwrap is not in PATH")
        if requested == "sandbox-exec" and not shutil.which("sandbox-exec"):
            raise IsolationError(
                "sandbox-exec isolation requested but sandbox-exec is not in PATH"
            )
        return requested

    def _validate_paths(self) -> None:
        if not self.enabled:
            return
        if self.codex_executable is None or not self.codex_executable.is_file():
            raise IsolationError("Could not resolve the Codex executable")
        if not self.project_root.is_dir():
            raise IsolationError(f"Project root is not a directory: {self.project_root}")
        if self.project_root == Path("/"):
            raise IsolationError("Refusing to expose the filesystem root as the project")
        if (
            self.run_private_dir != self.controller_state_root
            and self.controller_state_root not in self.run_private_dir.parents
        ):
            raise IsolationError(
                "Per-run private state must be inside the controller state root"
            )
        for path in self.extra_read_paths:
            if path == Path("/") or path == Path.home().resolve():
                raise IsolationError(
                    f"Refusing overly broad isolation read path: {path}"
                )
            if (
                path == self.controller_state_root
                or self.controller_state_root in path.parents
                or path in self.controller_state_root.parents
            ):
                raise IsolationError(
                    f"Controller state cannot overlap an agent read path: {path}"
                )
            if not path.exists():
                raise IsolationError(f"Isolation read path does not exist: {path}")

    def agent_dir(self, agent_name: str) -> Path:
        name = _safe_agent_name(agent_name)
        return self.run_private_dir / "agents" / name

    def prepare_agent(self, agent_name: str) -> Path:
        agent_dir = self.agent_dir(agent_name)
        codex_home = agent_dir / "codex-home"
        tmp_dir = agent_dir / "tmp"
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        # Mount targets must exist.  They are empty on the host; the real user
        # config/auth files are overlaid read-only only inside the namespace.
        for filename in ("auth.json", "config.toml"):
            target = codex_home / filename
            if not target.exists():
                target.touch(mode=0o600)
        for dirname in self.CODEX_RESOURCE_DIRS:
            (codex_home / dirname).mkdir(exist_ok=True)
        return agent_dir

    def paths(self, agent_name: str, output_file: Path) -> ExecutionPaths:
        if not self.enabled:
            if self.codex_executable is None:
                raise IsolationError("Codex executable is unavailable")
            return ExecutionPaths(
                codex=str(self.codex_executable),
                project_root=str(self.project_root),
                schema_file="",  # Caller uses the host schema path.
                output_file=str(output_file),
            )

        agent_dir = self.prepare_agent(agent_name)
        if self.backend == "sandbox-exec":
            try:
                output_file.resolve().relative_to(agent_dir.resolve())
            except ValueError as exc:
                raise IsolationError(
                    f"Agent output must be inside its private directory: {output_file}"
                ) from exc
            return ExecutionPaths(
                codex=str(self.codex_executable),
                project_root=str(self.project_root),
                schema_file="",
                output_file=str(output_file),
            )
        try:
            relative = output_file.resolve().relative_to(agent_dir.resolve())
        except ValueError as exc:
            raise IsolationError(
                f"Agent output must be inside its private directory: {output_file}"
            ) from exc
        return ExecutionPaths(
            codex="/codex",
            project_root="/workspace",
            schema_file="/output-schema.json",
            output_file=str(Path("/agent") / relative),
        )

    def wrap(
        self,
        *,
        agent_name: str,
        command: list[str],
        schema_file: Path,
        project_writable: bool,
    ) -> tuple[list[str], Optional[Mapping[str, str]]]:
        if not self.enabled:
            return command, None
        self.prepare_agent(agent_name)
        if self.backend == "bubblewrap":
            return self._bubblewrap_command(
                agent_name=agent_name,
                command=command,
                schema_file=schema_file,
                project_writable=project_writable,
            ), None
        if self.backend == "sandbox-exec":
            return self._sandbox_exec_command(
                agent_name=agent_name,
                command=command,
                schema_file=schema_file,
                project_writable=project_writable,
            )
        raise IsolationError(f"Unhandled isolation backend: {self.backend}")

    @staticmethod
    def _mkdir_args(path: Path) -> list[str]:
        args: list[str] = []
        current = Path("/")
        for part in path.parts[1:]:
            current /= part
            args.extend(["--dir", str(current)])
        return args

    @staticmethod
    def _system_mount_args() -> list[str]:
        args: list[str] = []
        for path in IsolationManager.SYSTEM_READ_PATHS:
            if path.exists():
                args.extend(["--ro-bind", str(path), str(path)])
        for path in IsolationManager.SYSTEM_LINK_PATHS:
            if path.is_symlink():
                args.extend(["--symlink", os.readlink(path), str(path)])
            elif path.exists():
                args.extend(["--ro-bind", str(path), str(path)])
        return args

    def _codex_home_mount_args(self, sandbox_home: Path, agent_home: Path) -> list[str]:
        args = self._mkdir_args(sandbox_home.parent)
        args.extend(["--bind", str(agent_home), str(sandbox_home)])
        for filename in ("auth.json", "config.toml"):
            source = self.codex_home / filename
            if source.is_file():
                args.extend(
                    ["--ro-bind", str(source), str(sandbox_home / filename)]
                )
        for dirname in self.CODEX_RESOURCE_DIRS:
            source = self.codex_home / dirname
            if source.is_dir():
                args.extend(
                    ["--ro-bind", str(source), str(sandbox_home / dirname)]
                )
        return args

    def _hidden_workspace_path(self) -> Optional[Path]:
        try:
            relative = self.controller_state_root.relative_to(self.project_root)
        except ValueError:
            return None
        return Path("/workspace") / relative

    def _bubblewrap_command(
        self,
        *,
        agent_name: str,
        command: list[str],
        schema_file: Path,
        project_writable: bool,
    ) -> list[str]:
        agent_dir = self.agent_dir(agent_name).resolve()
        agent_home = agent_dir / "codex-home"
        sandbox_home = self.codex_home

        args = [
            shutil.which("bwrap") or "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--hostname",
            f"pm-{_safe_agent_name(agent_name)}",
            "--clearenv",
        ]
        args.extend(self._system_mount_args())
        args.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
        args.extend(["--ro-bind", str(self.codex_executable), "/codex"])
        args.extend(["--ro-bind", str(schema_file.resolve()), "/output-schema.json"])
        project_flag = "--bind" if project_writable else "--ro-bind"
        args.extend([project_flag, str(self.project_root), "/workspace"])
        args.extend(["--bind", str(agent_dir), "/agent"])

        hidden = self._hidden_workspace_path()
        if hidden is not None and self.controller_state_root.exists():
            args.extend(["--tmpfs", str(hidden)])

        args.extend(self._codex_home_mount_args(sandbox_home, agent_home))
        for path in self.extra_read_paths:
            args.extend(self._mkdir_args(path.parent))
            args.extend(["--ro-bind", str(path), str(path)])

        env_values = {
            "HOME": str(sandbox_home.parent),
            "CODEX_HOME": str(sandbox_home),
            "TMPDIR": "/agent/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "TERM": "dumb",
        }
        for key in self.FORWARDED_ENV:
            value = os.environ.get(key)
            if value:
                env_values[key] = value
        for key, value in env_values.items():
            args.extend(["--setenv", key, value])
        args.extend(["--chdir", "/workspace", "--"])
        args.extend(command)
        return args

    @staticmethod
    def _seatbelt_literal(path: Path) -> str:
        value = str(path).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{value}"'

    def _sandbox_exec_command(
        self,
        *,
        agent_name: str,
        command: list[str],
        schema_file: Path,
        project_writable: bool,
    ) -> tuple[list[str], Mapping[str, str]]:
        agent_dir = self.agent_dir(agent_name).resolve()
        agent_home = agent_dir / "codex-home"

        # sandbox-exec does not provide a mount namespace.  A deny rule for the
        # controller root is therefore explicit and takes precedence over the
        # broader project allow rule.
        read_paths = [
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library"),
            self.project_root,
            agent_dir,
            self.codex_executable or Path("/usr/bin/false"),
            schema_file.resolve(),
            *self.extra_read_paths,
        ]
        for filename in ("auth.json", "config.toml"):
            source = self.codex_home / filename
            if source.exists():
                read_paths.append(source)
        for dirname in self.CODEX_RESOURCE_DIRS:
            source = self.codex_home / dirname
            if source.exists():
                read_paths.append(source)

        rules = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow network*)",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
        ]
        for path in _unique_paths(read_paths):
            operator = "subpath" if path.is_dir() else "literal"
            rules.append(
                f"(allow file-read* ({operator} {self._seatbelt_literal(path)}))"
            )
        rules.append(
            f"(deny file-read* (subpath {self._seatbelt_literal(self.controller_state_root)}))"
        )
        rules.append(
            f"(allow file-read* (subpath {self._seatbelt_literal(agent_dir)}))"
        )
        rules.append(
            f"(allow file-write* (subpath {self._seatbelt_literal(agent_dir)}))"
        )
        if project_writable:
            rules.append(
                f"(allow file-write* (subpath {self._seatbelt_literal(self.project_root)}))"
            )

        profile = agent_dir / "seatbelt.sb"
        profile.write_text("\n".join(rules) + "\n", encoding="utf-8")

        # Symlinks preserve the user's authentication/config without copying
        # credentials into retained checkpoint state.
        for filename in ("auth.json", "config.toml"):
            source = self.codex_home / filename
            target = agent_home / filename
            if source.exists():
                target.unlink(missing_ok=True)
                target.symlink_to(source)
        for dirname in self.CODEX_RESOURCE_DIRS:
            source = self.codex_home / dirname
            target = agent_home / dirname
            if source.exists():
                if target.is_dir() and not target.is_symlink():
                    try:
                        target.rmdir()
                    except OSError:
                        pass
                if not target.exists():
                    target.symlink_to(source, target_is_directory=True)

        env = {
            "HOME": str(agent_dir),
            "CODEX_HOME": str(agent_home),
            "TMPDIR": str(agent_dir / "tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "TERM": "dumb",
        }
        for key in self.FORWARDED_ENV:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return ["sandbox-exec", "-f", str(profile), "--", *command], env

    def self_test(self) -> None:
        """Exercise namespace/profile creation without invoking Codex."""
        if not self.enabled:
            raise IsolationError("Agent isolation is disabled")
        if self.backend == "bubblewrap":
            command = [
                shutil.which("bwrap") or "bwrap",
                "--die-with-parent",
                "--unshare-all",
                "--share-net",
                *self._system_mount_args(),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "/bin/true",
            ]
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit {result.returncode}"
                raise IsolationError(f"bubblewrap self-test failed: {detail}")
            return
        if self.backend == "sandbox-exec":
            result = subprocess.run(
                ["sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit {result.returncode}"
                raise IsolationError(f"sandbox-exec self-test failed: {detail}")
            return
        raise IsolationError(f"Unknown isolation backend: {self.backend}")

    def describe(self) -> str:
        if not self.enabled:
            return "disabled"
        extras = ", ".join(shlex.quote(str(p)) for p in self.extra_read_paths)
        return self.backend + (f" (extra reads: {extras})" if extras else "")
