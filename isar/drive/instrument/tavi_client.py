"""Stdlib TAVI REST client (WORKING — LLM_HARNESS_DESIGN §2.3; API in TAVI docs/API_USER_GUIDE.md).

A thin, dependency-free wrapper over TAVI's ``http://127.0.0.1:8642/api/v1`` surface. ISAR is
stdlib+numpy/scipy only, so this uses ``urllib`` — no ``requests``/``httpx``. JSON in, JSON out;
errors raise :class:`TaviError` carrying the HTTP status and the response body text (honest failures,
never a swallowed error). ``409``/``429``/``503`` are retried a capped number of times, obeying the
``Retry-After`` header (API_USER_GUIDE §5).

The client is a mechanical wrapper — it holds no physics and makes no decision. It is the driver's
write path; the same verbs (schema → state → validate → scan → wait → data) map to a real TAS later.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8642/api/v1"

# Terminal job states (API_USER_GUIDE §5 long-poll). A wait ends when the job reaches one of these.
TERMINAL_STATES = frozenset({"done", "failed", "stopped", "cancelled"})

# Statuses whose Retry-After we obey with a bounded retry (API_USER_GUIDE §5 Retry-After).
_RETRYABLE = frozenset({409, 429, 503})


class TaviError(RuntimeError):
    """A TAVI API call failed. Carries the HTTP status and the response body text (when available)."""

    def __init__(self, status: int | None, message: str, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(message)


class TaviTimeout(TaviError):
    """A long-poll (:meth:`TaviClient.wait_scan`) did not reach a terminal state within the deadline."""

    def __init__(self, message: str) -> None:
        super().__init__(status=None, message=message)


class TaviClient:
    """A client for one TAVI instance.

    Args:
        base_url: API base, default :data:`DEFAULT_BASE_URL`.
        timeout: per-request socket timeout (seconds). ``wait_scan`` adds the long-poll ``wait`` on top.
        max_retries: how many times to obey a ``Retry-After`` on a 409/429/503 before giving up.
        retry_cap: cap (seconds) on any single ``Retry-After`` sleep, so a hostile hint cannot hang us.
        sleep: injectable sleep (tests pass a no-op); defaults to :func:`time.sleep`.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 30.0,
                 max_retries: int = 4, retry_cap: float = 30.0, sleep=time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.retry_cap = float(retry_cap)
        self._sleep = sleep

    # ---- read endpoints ----------------------------------------------------
    def get_schema(self) -> dict[str, Any]:
        """``GET /schema`` — the fields, allowed values, limits, and engines for THIS instrument."""
        return self._request("GET", "/schema")

    def get_state(self) -> dict[str, Any]:
        """``GET /state`` — current parameters + ``mode`` (confirm ``"allow"``) and ``busy``."""
        return self._request("GET", "/state")

    def get_resolution(self, h: float, k: float, l: float, deltaE: float,
                       method: str = "auto") -> dict[str, Any]:
        """``GET /resolution`` — theoretical Cooper–Nathans/Popovici resolution at one point.

        Read-only; used only for the advisory step width (LLM_HARNESS_DESIGN §8 step 4.2), never for a
        conclusion. ``method`` is ``auto`` | ``cooper_nathans`` | ``popovici``.
        """
        params = {"H": h, "K": k, "L": l, "deltaE": deltaE, "method": method}
        return self._request("GET", "/resolution", params=params)

    def get_scan_data(self, job_id: str) -> dict[str, Any]:
        """``GET /scan/{id}/data`` — the job snapshot WITH the full ``result`` arrays.

        The returned dict is what :func:`isar.drive.isar_io.scan_writer.save_scan_json` persists
        verbatim for ISAR's ``tavi`` parser plugin (it sniffs top-level ``job_id`` + ``result``).
        """
        return self._request("GET", f"/scan/{self._quote(job_id)}/data")

    def get_scan(self, job_id: str, *, wait: float | None = None) -> dict[str, Any]:
        """``GET /scan/{id}`` — the job snapshot (no arrays). ``wait`` long-polls up to N seconds."""
        params = {"wait": wait} if wait is not None else None
        return self._request("GET", f"/scan/{self._quote(job_id)}", params=params)

    # ---- write / dry-run endpoints ----------------------------------------
    def validate(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /validate`` — dry-run the exact checks ``POST /scan`` performs; never queues/mutates.

        Returns the ``validation`` object plus ``would_queue`` (bool) and ``blockers`` (list of str).
        """
        return self._request("POST", "/validate", body=body)

    def submit_scan(self, body: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """``POST /scan`` — submit a scan job; returns the 202 payload ``{job_id, state, ...}``.

        Pass ``idempotency_key`` to reuse an ``Idempotency-Key`` header on an uncertain retry.
        The key is escaped to ASCII (http.client encodes header values latin-1; campaign names
        can carry arbitrary unicode from the goal text).
        """
        if idempotency_key:
            idempotency_key = idempotency_key.encode("ascii", "backslashreplace").decode("ascii")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/scan", body=body, headers=headers)

    def wait_scan(self, job_id: str, timeout: float = 120.0, *, poll_wait: float = 30.0) -> dict[str, Any]:
        """Long-poll ``GET /scan/{id}?wait=N`` until the job reaches a terminal state.

        Repeats the long-poll (each blocking up to ``poll_wait`` seconds, clamped to TAVI's 120 s max)
        until the job ``state`` is terminal (``done``/``failed``/``stopped``/``cancelled``) or the
        overall ``timeout`` elapses. Returns the terminal snapshot. Raises :class:`TaviTimeout` if the
        deadline passes first. Does NOT judge success — a ``failed`` job returns normally; the caller
        inspects ``state``/``error``.
        """
        deadline = time.monotonic() + float(timeout)
        wait_n = min(float(poll_wait), 120.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaviTimeout(
                    f"scan {job_id} did not reach a terminal state within {timeout}s "
                    f"(last state unknown)")
            snap = self.get_scan(job_id, wait=min(wait_n, remaining))
            state = str(snap.get("state", ""))
            if state in TERMINAL_STATES:
                return snap
            # timed_out True means the wait expired with the job still running/queued — loop again.

    # ---- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, headers: dict | None = None) -> dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Accept": "application/json"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310 (localhost)
                    raw = resp.read()
                return _decode_json(raw, url)
            except urllib.error.HTTPError as exc:
                status = exc.code
                err_body = _read_error_body(exc)
                if status in _RETRYABLE and attempt < self.max_retries:
                    delay = _retry_after(exc, cap=self.retry_cap)
                    attempt += 1
                    self._sleep(delay)
                    continue
                raise TaviError(status, f"{method} {url} -> HTTP {status}: {err_body}",
                                body=err_body) from exc
            except urllib.error.URLError as exc:
                raise TaviError(None, f"{method} {url} -> transport error: {exc.reason}") from exc

    @staticmethod
    def _quote(job_id: str) -> str:
        return urllib.parse.quote(str(job_id), safe="")


def _decode_json(raw: bytes, url: str) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TaviError(None, f"{url} -> non-JSON response: {exc}", body=raw[:500].decode("utf-8", "replace"))


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _retry_after(exc: urllib.error.HTTPError, *, cap: float) -> float:
    """The Retry-After delay (seconds), clamped to ``[0, cap]``. Defaults to a small delay if absent."""
    hdr = exc.headers.get("Retry-After") if exc.headers else None
    try:
        secs = float(hdr) if hdr is not None else 1.0
    except (TypeError, ValueError):
        secs = 1.0
    return max(0.0, min(secs, cap))
