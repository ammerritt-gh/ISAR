"""``python -m isar_gui <workspace>`` — the module entry point, equivalent to ``isar view``."""
from __future__ import annotations

import sys

from isar_gui.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
