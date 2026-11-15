"""Shared native job store for the standalone workbench and video pipeline."""

from functools import lru_cache
from pathlib import Path

from novelvideo.config import OUTPUT_DIR, STATE_DIR

from .client import NativeClient
from .jobs import JobStore


@lru_cache(maxsize=1)
def store() -> JobStore:
    return JobStore(
        Path(STATE_DIR) / "local/doubao-work",
        Path(OUTPUT_DIR) / "doubao-work",
        NativeClient(),
    )
