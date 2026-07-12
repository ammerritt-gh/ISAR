"""Shared locations of the frozen read-only baselines (skip-if-missing by convention)."""
import os
from pathlib import Path

_DEFAULT_DIR = Path(r"C:\Users\AMM\Documents\ISAR_baselines")


def baselines_dir() -> Path:
    """The baselines root: ISAR_BASELINES_DIR env override, else the default location."""
    env = os.environ.get("ISAR_BASELINES_DIR")
    return Path(env) if env else _DEFAULT_DIR


def mvp16_path() -> Path:
    return baselines_dir() / "mvp_zone_test16"
