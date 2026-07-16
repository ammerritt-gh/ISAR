"""Qt-free backend health checks and safe asynchronous probe coordination.

The GUI must never let a Python worker emit through a Qt object: Qt can delete the receiver while
the worker is still running during shutdown.  :class:`HealthProbe` consequently uses only standard
library synchronization and a result queue.  The GUI owns the timer that polls the queue, so a late
worker result after the window closes is inert.
"""
from __future__ import annotations

import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

# Default backend locations (the "default profile", overridable in Settings — never assumed present).
LLM_DEFAULT_ENDPOINT = "http://127.0.0.1:8766"
TAVI_DEFAULT_URL = "http://127.0.0.1:8642/api/v1"


def tavi_health_url(base: str) -> str:
    """The liveness URL for a TAVI API base: ``<base>/health``.

    TAVI routes only subpaths — its bare API root returns 404 — so probing the base itself
    reports a perfectly healthy server as ``reachable_error`` (amber dot) and blocks campaign
    launch. ``/api/v1/health`` is the designed liveness endpoint (200 with
    ``{"status": "ok", "instrument": ...}``)."""
    return base.rstrip("/") + "/health"


@dataclass(frozen=True)
class Health:
    """One endpoint's state.

    ``ok`` remains the compatibility property used by older callers.  ``reachable`` distinguishes a
    responding endpoint that returned an HTTP error (amber) from a transport failure (red).
    """

    ok: bool
    detail: str
    reachable: bool | None = None

    def __post_init__(self) -> None:
        if self.reachable is None:
            object.__setattr__(self, "reachable", self.ok)

    @property
    def status(self) -> str:
        """Return ``ok``, ``reachable_error``, or ``down`` for human-facing rendering."""
        if self.ok:
            return "ok"
        if self.reachable:
            return "reachable_error"
        return "down"


def ping_http(url: str, timeout: float = 1.5, opener=None) -> Health:
    """Probe ``url`` without treating a HTTP error response as healthy.

    Any 2xx response is healthy.  HTTP 3xx/4xx/5xx means the server is reachable but unsuitable for
    the requested endpoint; connection failures are down.  ``opener`` is injected in tests.
    """
    opener = opener or urllib.request.urlopen
    try:
        response = opener(url, timeout=timeout)
        status = getattr(response, "status", 200)
        if 200 <= status < 300:
            return Health(True, "reachable")
        return Health(False, f"reachable (HTTP {status})", reachable=True)
    except urllib.error.HTTPError as exc:
        return Health(False, f"reachable (HTTP {exc.code})", reachable=True)
    except Exception as exc:  # noqa: BLE001 — any transport failure means "down"
        return Health(False, f"unreachable ({type(exc).__name__})", reachable=False)


@dataclass(frozen=True)
class HealthProbeResult:
    """A completed immutable request, safe to transfer through a Python queue."""

    generation: int
    urls: dict[str, str]
    health: dict[str, Health] | None = None
    error: str | None = None


class HealthProbe:
    """Coalesced, one-at-a-time health probe with no Qt dependency.

    A request made while another is running replaces the one pending request.  The caller tags its
    UI state with the returned generation and discards older results.  ``shutdown`` deliberately
    does not join a blocked network call; it only ensures no late result can schedule more work.
    """

    def __init__(self, prober: Callable[[str], Health] | None = None) -> None:
        self._prober = prober or ping_http
        self._results: queue.SimpleQueue[HealthProbeResult] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._generation = 0
        self._in_flight = False
        self._pending: tuple[int, dict[str, str]] | None = None
        self._shutdown = False

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._in_flight

    def request(self, urls: Mapping[str, str]) -> int:
        """Schedule a probe and return its monotonically increasing generation."""
        request_urls = dict(urls)
        with self._lock:
            if self._shutdown:
                return self._generation
            self._generation += 1
            request = (self._generation, request_urls)
            if self._in_flight:
                self._pending = request
            else:
                self._start_locked(request)
            return self._generation

    def poll(self) -> list[HealthProbeResult]:
        """Return completed results and schedule the newest coalesced request, if any."""
        completed: list[HealthProbeResult] = []
        while True:
            try:
                completed.append(self._results.get_nowait())
            except queue.Empty:
                break
        if not completed:
            return completed
        with self._lock:
            self._in_flight = False
            if not self._shutdown and self._pending is not None:
                pending = self._pending
                self._pending = None
                self._start_locked(pending)
        return completed

    def shutdown(self) -> None:
        """Make current or late worker results inert without blocking the GUI shutdown path."""
        with self._lock:
            self._shutdown = True
            self._pending = None

    def _start_locked(self, request: tuple[int, dict[str, str]]) -> None:
        self._in_flight = True
        threading.Thread(target=self._work, args=request, daemon=True,
                         name="isar-gui-health").start()

    def _work(self, generation: int, urls: dict[str, str]) -> None:
        try:
            health = {name: self._prober(url) for name, url in urls.items()}
            result = HealthProbeResult(generation, urls, health=health)
        except Exception as exc:  # noqa: BLE001 — a broken probe must never kill the worker
            result = HealthProbeResult(generation, urls, error=f"{type(exc).__name__}: {exc}")
        self._results.put(result)


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
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None
