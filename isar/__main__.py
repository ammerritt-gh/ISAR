"""Module entry point so ``python -m isar ...`` works (the GUI on-ramp shells the engine this way,
via ``sys.executable -m isar``, which is more robust than depending on the ``isar`` console script
being on PATH in the subprocess environment)."""
from __future__ import annotations

from isar.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
