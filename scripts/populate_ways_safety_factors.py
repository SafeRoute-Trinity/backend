#!/usr/bin/env python3
"""
Refresh saferoute.ways_safety_view and batch-populate public.ways.safety_factor.

Same logic as the safety_scoring service background job: materialized view refresh
(one statement), then checkpointed batched UPDATEs into ways (see
libs.safety_ways_factor_sync.py).

Usage (from backend repo root):

  python scripts/populate_ways_safety_factors.py

Requires POSTGIS_DATABASE_URL or SAFETY_SCORING_DATABASE_URL in the environment
(or backend/.env).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))


def main() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]

    env_path = repo_root / ".env"
    if load_dotenv and env_path.exists():
        load_dotenv(env_path)

    if not (os.getenv("POSTGIS_DATABASE_URL") or os.getenv("SAFETY_SCORING_DATABASE_URL")):
        print(
            "Set POSTGIS_DATABASE_URL or SAFETY_SCORING_DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    from services.safety_scoring.main import _refresh_safety_scores

    asyncio.run(_refresh_safety_scores())


if __name__ == "__main__":
    main()
