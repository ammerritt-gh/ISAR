"""Shared setup for the A-10 characterization goldens.

Forces the Qt offscreen platform BEFORE any Qt import so the goldens build headless and xdist-safe
(the view models are QObjects; building their specs constructs QObjects). No workspace factory is
exposed here — the goldens pin the frozen MVP-16 baseline, which each test opens read-only.
"""
from __future__ import annotations

import os

# Must precede any PySide6 import (the view models pull PySide6 at module scope).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
