"""`install.sh`'s interpreter detection.

The script exists to stop a stock macOS `python3` (3.9) being used for a
tool whose MCP SDK needs 3.10+. The $PYTHON override bypassed the very
checks it applies to every other candidate, so a stale value - common
after a venv is deleted - defeated the whole guard and the script died
later at `pip` with a bare "command not found".
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from conftest import REPO_ROOT

INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


def run_find_python(tmp_path, **env):
    """Call install.sh's find_python in isolation.

    The function bodies are extracted in Python and written to a file that
    bash sources - doing the extraction inside `bash -c` means nesting
    quotes around a sed program containing braces, which the shell mangles.
    """
    lines = open(INSTALL_SH).read().splitlines()
    out, keep = [], False
    for line in lines:
        if re.match(r"^[a-z_]+\(\) \{$", line):
            keep = True
        if keep:
            out.append(line)
        if keep and line == "}":
            keep = False
    helpers = tmp_path / "helpers.sh"
    helpers.write_text("\n".join(out) + "\n")
    assert "find_python()" in helpers.read_text(), "extraction found no functions"

    proc = subprocess.run(
        ["bash", "-c", f'. "{helpers}"; out="$(find_python)"; echo "OUT=$out"'],
        capture_output=True, text=True, env={**os.environ, **env},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_install_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", INSTALL_SH]).returncode == 0


def test_finds_a_suitable_interpreter_by_default(tmp_path):
    out = run_find_python(tmp_path, PYTHON="")
    assert "OUT=" in out and out.split("OUT=", 1)[1].strip(), out


def test_a_nonexistent_PYTHON_override_is_rejected(tmp_path):
    """The regression: this used to be returned verbatim."""
    assert run_find_python(tmp_path, PYTHON=str(tmp_path / "does-not-exist")).endswith("OUT=")


def test_a_too_old_PYTHON_override_is_rejected(tmp_path):
    """A real interpreter that is simply too old must also be rejected -
    that is the case the script was written for."""
    fake = tmp_path / "python-old"
    fake.write_text("#!/bin/sh\nexit 1\n")   # fails the >=3.10 version probe
    fake.chmod(0o755)
    assert run_find_python(tmp_path, PYTHON=str(fake)).endswith("OUT=")


def test_a_valid_PYTHON_override_is_honoured(tmp_path):
    import sys
    if sys.version_info < (3, 10):
        pytest.skip("this interpreter is older than 3.10, so it is correctly rejected")
    out = run_find_python(tmp_path, PYTHON=sys.executable)
    assert out.endswith(f"OUT={sys.executable}")
