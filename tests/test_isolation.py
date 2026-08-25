from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime_isolation import IsolationManager


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
class IsolationCommandTest(unittest.TestCase):
    def test_codex_code_mode_host_is_mounted_when_shipped_beside_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            state = root / "state"
            run = state / "run"
            bin_dir = root / "bin"
            project.mkdir()
            run.mkdir(parents=True)
            bin_dir.mkdir()
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            codex = bin_dir / "codex"
            companion = bin_dir / "codex-code-mode-host"
            for executable in (codex, companion):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)

            manager = IsolationManager(
                enabled=True,
                backend="bubblewrap",
                project_root=project,
                controller_state_root=state,
                run_private_dir=run,
                codex_executable=codex,
            )
            command, _ = manager.wrap(
                agent_name="momus",
                command=["/codex"],
                schema_file=schema,
                project_writable=False,
            )

            index = command.index("/codex-code-mode-host")
            self.assertEqual(command[index - 2], "--ro-bind")
            self.assertEqual(command[index - 1], str(companion.resolve()))


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
class BubblewrapIsolationTest(unittest.TestCase):
    def test_controller_state_and_other_agent_are_not_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            state = project / "controller-state"
            run = state / "run"
            project.mkdir()
            state.mkdir()
            run.mkdir()
            (state / "secret.txt").write_text("hidden", encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            extra = root / "narrow-extra.txt"
            extra.write_text("allowed", encoding="utf-8")

            manager = IsolationManager(
                enabled=True,
                backend="bubblewrap",
                project_root=project,
                controller_state_root=state,
                run_private_dir=run,
                codex_executable=Path("/bin/true"),
                extra_read_paths=(extra,),
            )
            agent = manager.prepare_agent("momus")
            (agent / "visible.txt").write_text("visible", encoding="utf-8")
            manager.prepare_agent("prometheus")
            paths = manager.paths("momus", agent / "latest.json")
            self.assertEqual(paths.project_root, "/workspace")
            self.assertEqual(paths.output_file, "/agent/latest.json")

            command, environment = manager.wrap(
                agent_name="momus",
                command=[
                    "/bin/sh",
                    "-c",
                    "test ! -e /workspace/controller-state/secret.txt "
                    "&& test -e /agent/visible.txt "
                    "&& test ! -e /agent/../prometheus "
                    f"&& test -r {extra}",
                ],
                schema_file=schema,
                project_writable=False,
            )
            result = subprocess.run(
                command,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
