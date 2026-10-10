"""Importing the api package must export .env into os.environ.

Settings read .env through pydantic-settings, which never touches os.environ,
while the Meta OAuth builder and the capabilities inspector read os.getenv.
Without the bootstrap the API answers "META_APP_ID is not configured".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def _env_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value.strip():
            keys.add(name.strip())
    return keys


@pytest.mark.skipif(not ENV_FILE.exists(), reason="no local .env to export")
def test_importing_api_exports_env_file_into_os_environ():
    wanted = sorted(_env_keys() & {"META_APP_ID", "META_APP_SECRET", "ENCRYPTION_KEY"})
    if not wanted:
        pytest.skip(".env defines none of the Meta bootstrap keys")

    # A fresh interpreter is required: the import side effect only happens once
    # per process, and pytest may already have imported api.
    code = (
        "import api, os, json;"
        f"print(json.dumps({{k: bool(os.getenv(k)) for k in {wanted!r}}}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    exported = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(exported.values()), f"not exported into os.environ: {exported}"
