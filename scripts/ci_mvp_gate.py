"""Run Reflexo's deterministic MVP release gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


GATE_TESTS = (
    "tests/eval",
    "tests/test_routing.py",
    "tests/test_temporal_only_routing.py",
    "tests/test_workflow_stateful_testing.py",
    "tests/test_workflow_test_jobs.py",
    "tests/test_simulator_diagnostics.py",
    "tests/test_voice_stage10.py",
    "tests/test_channel_framework_foundation.py",
    "tests/test_channel_framework_runtime.py",
    "tests/test_connection_hub.py",
    "tests/test_no_nango.py",
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "pytest",
        *GATE_TESTS,
        "-q",
        "--tb=short",
    ]
    mode = "live eval enabled" if os.getenv("RUN_EVAL") == "1" else "live eval skipped"
    print(f"Reflexo MVP gate: {mode}", flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
