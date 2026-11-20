from __future__ import annotations

import io
from pathlib import Path

import pexpect

from tests.e2e.common import wait_for_rendered_text


def test_rendered_reply_is_found_behind_terminal_redraw_backlog(tmp_path: Path):
    script = tmp_path / "redraw.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write(('\\x1b[1;1H' + 'frame ' * 200 + '\\n') * 250)\n"
        "sys.stdout.write('FINAL-REPLY-CANARY\\n')\n"
        "sys.stdout.flush()\n"
    )
    captured = io.StringIO()
    child = pexpect.spawn(
        "uv", ["run", "--no-sync", "python", str(script)], encoding="utf-8", timeout=3
    )
    child.logfile_read = captured
    try:
        wait_for_rendered_text(child, captured, "FINAL-REPLY-CANARY", timeout=2)
    finally:
        if child.isalive():
            child.terminate(force=True)
        child.close()
