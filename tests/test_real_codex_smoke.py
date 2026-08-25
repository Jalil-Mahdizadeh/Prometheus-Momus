from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from debate import get_thread_id
from runtime_isolation import IsolationManager


@unittest.skipUnless(
    os.environ.get("PROMETHEUS_MOMUS_REAL_CODEX_SMOKE") == "1",
    "set PROMETHEUS_MOMUS_REAL_CODEX_SMOKE=1 to spend two real Codex calls",
)
class RealCodexResumeSchemaSmokeTest(unittest.TestCase):
    def test_structured_output_survives_isolated_resume(self):
        codex = shutil.which("codex")
        self.assertIsNotNone(codex)
        repository = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            run = state / "run"
            run.mkdir(parents=True)
            schema = root / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    }
                ),
                encoding="utf-8",
            )
            isolation = IsolationManager(
                enabled=True,
                backend="auto",
                project_root=repository,
                controller_state_root=state,
                run_private_dir=run,
                codex_executable=Path(codex),
            )
            isolation.self_test()
            agent_dir = isolation.prepare_agent("smoke")
            model = os.environ.get("PROMETHEUS_MOMUS_SMOKE_MODEL")

            def invoke(
                output_file: Path,
                prompt: str,
                thread_id: str | None = None,
            ) -> subprocess.CompletedProcess:
                paths = isolation.paths("smoke", output_file)
                command = [
                    paths.codex,
                    "exec",
                    "-C",
                    paths.project_root,
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--json",
                    "--output-schema",
                    paths.schema_file or str(schema),
                    "--output-last-message",
                    paths.output_file,
                ]
                if model:
                    command.extend(["--model", model])
                if thread_id is None:
                    command.append("-")
                else:
                    command.extend(["resume", thread_id, "-"])
                wrapped, environment = isolation.wrap(
                    agent_name="smoke",
                    command=command,
                    schema_file=schema,
                    project_writable=False,
                )
                return subprocess.run(
                    wrapped,
                    env=environment,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300,
                )

            first_output = agent_dir / "first.json"
            first = invoke(
                first_output,
                'Return {"message":"first"} and nothing else.',
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            thread_id = get_thread_id(first.stdout)
            self.assertEqual(
                json.loads(first_output.read_text(encoding="utf-8"))["message"],
                "first",
            )

            second_output = agent_dir / "second.json"
            second = invoke(
                second_output,
                'Return {"message":"second"} and nothing else.',
                thread_id,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                json.loads(second_output.read_text(encoding="utf-8"))["message"],
                "second",
            )


if __name__ == "__main__":
    unittest.main()
