"""Qt-free backend reachability checks for the GUI status indicators (D1/D3).

ISAR's analysis half runs with no backends; the campaign half needs TAVI, and ``--llm`` needs an LLM
endpoint. These helpers let the GUI show a clear reachable/unreachable status (never a crash) without
importing the engine (G16) — they are plain stdlib ``urllib`` pings, so they unit-test with an
injected opener and never touch the network in CI.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Default backend locations (the "default profile", overridable in Settings — never assumed present).
LLM_DEFAULT_ENDPOINT = "http://127.0.0.1:8766"
TAVI_DEFAULT_URL = "http://127.0.0.1:8642/api/v1"


@dataclass(frozen=True)
class Health:
    """Reachability of one backend, plus a human detail string for the tooltip."""

    ok: bool
    detail: str


def ping_http(url: str, timeout: float = 1.5, opener=None) -> Health:
    """Probe ``url`` for reachability. A server that answers at all — even 401/404 — is *reachable*;
    only a connection failure (refused / DNS / timeout) is *down*. ``opener`` is injected in tests.
    """
    opener = opener or urllib.request.urlopen
    try:
        opener(url, timeout=timeout)
        return Health(True, "reachable")
    except urllib.error.HTTPError as exc:      # the server answered with a status → it is up
        return Health(True, f"reachable (HTTP {exc.code})")
    except Exception as exc:                    # noqa: BLE001 — any transport failure means "down"
        return Health(False, f"unreachable ({type(exc).__name__})")


def find_tavi_bat(configured: str | None = None) -> str | None:
    """Resolve TAVI's ``run-tavi-dev.bat`` (for the Launch TAVI button): the configured path if it
    exists, else a couple of conventional locations, else ``None`` (Launch is then disabled)."""
    candidates = []
    if configured:
        candidates.append(Path(configured))
    home = Path.home()
    candidates += [
        home / "Documents" / "Github" / "TAVI" / "run-tavi-dev.bat",
        home / "TAVI" / "run-tavi-dev.bat",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None
