#!/usr/bin/env python3
"""Minimal Streamable-HTTP MCP server for Stage 11 tests and live smoke.

Usage:
  python -m tests.fixtures.fake_crm_mcp --port 8099
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastmcp import FastMCP

mcp = FastMCP(
    "fake-crm",
    instructions="Test CRM for Reflexo Stage 11. One lookup tool.",
)


@mcp.tool()
async def lookup_customer(name: str) -> dict:
    """Look up a CRM customer by name."""
    key = (name or "").strip().lower()
    if "rahul" in key:
        return {
            "name": "Rahul",
            "status": "active",
            "plan": "Pro",
            "last_invoice": "paid",
        }
    return {"name": name, "status": "unknown"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
