"""Thread id enumeration for scripts/reset_owner_threads.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reset_owner_threads", ROOT / "scripts" / "reset_owner_threads.py"
)
reset_owner_threads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset_owner_threads)

BIZ = "918750d7-2254-4cfd-bd02-c22c3ab708ec"


def test_owner_threads_cover_broker_and_config_agent():
    threads = reset_owner_threads.thread_ids_for(BIZ, "whatsapp", None)
    assert f"{BIZ}:owner:whatsapp" in threads
    assert f"{BIZ}:owner:whatsapp:broker" in threads
    assert f"{BIZ}:owner:whatsapp:spec:config_agent" in threads
    assert f"{BIZ}:owner:whatsapp:spec:knowledge_agent" in threads
    assert len(threads) == len(set(threads))


def test_customer_threads_use_customer_specialists():
    threads = reset_owner_threads.thread_ids_for(BIZ, "whatsapp", "cust-1")
    assert f"{BIZ}:cust-1:whatsapp" in threads
    assert f"{BIZ}:cust-1:whatsapp:spec:inquiry_agent" in threads
    assert f"{BIZ}:cust-1:whatsapp:spec:support_agent" in threads
    assert not any("owner" in t for t in threads)
