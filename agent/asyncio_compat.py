"""Asyncio helpers for Windows + psycopg compatibility."""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """
    Run a coroutine, using SelectorEventLoop on Windows.

    psycopg async (LangGraph AsyncPostgresSaver) cannot use ProactorEventLoop,
    which is the default on Windows for asyncio.run().
    """
    if sys.platform == "win32":

        def _factory() -> asyncio.AbstractEventLoop:
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

        return asyncio.run(coro, loop_factory=_factory)
    return asyncio.run(coro)
