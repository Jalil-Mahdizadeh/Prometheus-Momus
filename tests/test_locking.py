from __future__ import annotations

import contextlib
import fcntl
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from debate import DebateController


class ControllerLockTests(unittest.TestCase):
    def controller(self, path):
        controller = DebateController.__new__(DebateController)
        controller.lock_path = path
        controller._lock_handle = None
        controller.resuming = False
        return controller

    def test_preopened_contender_and_new_process_share_one_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".debate.lock"
            owner = self.controller(path)
            owner.acquire_lock()
            # A contender may open the file before the previous owner releases it.
            with path.open("r+") as contender:
                owner.release_lock()
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [sys.executable, "-c", """
import fcntl
import sys
with open(sys.argv[1], 'a+') as handle:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
""", str(path)], capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_contender_does_not_truncate_owner_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".debate.lock"
            owner, contender = self.controller(path), self.controller(path)
            owner.acquire_lock()
            try:
                before = path.read_text()
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        contender.acquire_lock()
                self.assertEqual(path.read_text(), before)
                contender.release_lock()
            finally:
                owner.release_lock()


if __name__ == "__main__":
    unittest.main()
