"""TaviClient — unit-tested against a stdlib http.server stub (no live TAVI required)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from isar.drive.instrument.tavi_client import TaviClient, TaviError, TaviTimeout


class _StubState:
    """Mutable state the handler reads, so tests can script behavior (retries, wait progression)."""
    def __init__(self):
        self.scan_calls = 0        # how many times GET /scan/{id} was hit
        self.flaky_calls = 0       # how many times the flaky endpoint was hit
        self.last_post_body = None
        self.retry_after_hits = 0
        self.last_put_body = None
        self.last_put_path = None


def _make_handler(state: _StubState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path == "/api/v1/schema":
                return self._send(200, {"engines": ["mcstas", "deterministic"], "fields": {}})
            if path == "/api/v1/state":
                return self._send(200, {"mode": "allow", "busy": False, "parameters": {"H": 2.0}})
            if path == "/api/v1/resolution":
                return self._send(200, {"H": float(qs["H"][0]), "deltaE": float(qs["deltaE"][0]),
                                        "method": qs.get("method", ["auto"])[0], "fwhm": 0.5})
            if path == "/api/v1/background":
                return self._send(200, {"enabled": False, "preset": "none",
                                        "profile_fingerprint": "0123456789abcdef", "terms": []})
            if path == "/api/v1/scan/j-0001/data":
                return self._send(200, {"job_id": "j-0001", "state": "done",
                                        "result": {"scan_values_1": [1, 2], "counts": [10, 20]}})
            if path == "/api/v1/scan/j-0001":
                # First poll: still running (timed_out). Second: done.
                state.scan_calls += 1
                if state.scan_calls < 2:
                    return self._send(200, {"job_id": "j-0001", "state": "running", "timed_out": True})
                return self._send(200, {"job_id": "j-0001", "state": "done", "timed_out": False})
            if path == "/api/v1/flaky":
                state.flaky_calls += 1
                if state.flaky_calls < 3:
                    state.retry_after_hits += 1
                    return self._send(503, {"error": {"code": "gui_busy"}}, {"Retry-After": "0"})
                return self._send(200, {"ok": True, "calls": state.flaky_calls})
            if path in ("/api/v1/scan/nope", "/api/v1/scan/nope/data"):
                return self._send(404, {"error": {"code": "unknown_job", "message": "no such job"}})
            return self._send(404, {"error": {"code": "not_found", "message": path}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state.last_post_body = json.loads(self.rfile.read(length) or b"{}")
            u = urlparse(self.path)
            if u.path == "/api/v1/validate":
                return self._send(200, {"points": 5, "would_queue": True, "blockers": []})
            if u.path == "/api/v1/scan":
                return self._send(202, {"job_id": "j-0001", "state": "queued", "position": 0})
            return self._send(404, {"error": {"code": "not_found"}})

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            state.last_put_body = json.loads(self.rfile.read(length) or b"{}")
            state.last_put_path = urlparse(self.path).path
            if state.last_put_path == "/api/v1/background":
                return self._send(200, {"enabled": True, "preset": "flat_low",
                                        "profile_fingerprint": "aaaabbbbccccdddd"})
            return self._send(404, {"error": {"code": "not_found"}})
    return Handler


@pytest.fixture()
def server():
    state = _StubState()
    httpd = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    t = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    t.start()
    host, port = httpd.server_address
    base = f"http://{host}:{port}/api/v1"
    try:
        yield base, state
    finally:
        httpd.shutdown()
        t.join(timeout=2.0)
        assert not t.is_alive()
        httpd.server_close()


def _client(base, **kw):
    # No real sleeping in retry tests.
    return TaviClient(base, sleep=lambda s: None, **kw)


def test_get_schema(server):
    base, _ = server
    schema = _client(base).get_schema()
    assert "deterministic" in schema["engines"]


def test_get_state(server):
    base, _ = server
    st = _client(base).get_state()
    assert st["mode"] == "allow" and st["busy"] is False


def test_get_resolution_passes_params(server):
    base, _ = server
    res = _client(base).get_resolution(2.0, 0.0, 0.0, 1.5, method="cooper_nathans")
    assert res["H"] == 2.0 and res["deltaE"] == 1.5 and res["method"] == "cooper_nathans"


def test_validate(server):
    base, state = server
    out = _client(base).validate({"parameters": {"scan_command1": "deltaE 0 3 0.5"}})
    assert out["would_queue"] is True
    assert state.last_post_body["parameters"]["scan_command1"] == "deltaE 0 3 0.5"


def test_submit_scan(server):
    base, _ = server
    out = _client(base).submit_scan({"engine": "deterministic", "seed": 1})
    assert out["job_id"] == "j-0001" and out["state"] == "queued"


def test_submit_scan_sends_idempotency_key(server):
    base, _ = server
    # No assertion on the header echo (stub ignores it) — just that it does not error.
    out = _client(base).submit_scan({}, idempotency_key="run-a")
    assert out["job_id"] == "j-0001"


def test_get_background(server):
    base, _ = server
    profile = _client(base).get_background()
    assert profile["enabled"] is False and profile["profile_fingerprint"] == "0123456789abcdef"


def test_set_background_puts_the_spec_verbatim(server):
    base, state = server
    spec = {"enabled": True, "preset": "flat_low", "overrides": {"flat": {"rate": 2.0e-10}}}
    out = _client(base).set_background(spec)
    assert state.last_put_path == "/api/v1/background"     # PUT, not POST, at /background
    assert state.last_put_body == spec                     # body is the spec, unwrapped
    assert out["profile_fingerprint"] == "aaaabbbbccccdddd"


def test_get_scan_data(server):
    base, _ = server
    data = _client(base).get_scan_data("j-0001")
    assert data["result"]["counts"] == [10, 20]


def test_wait_scan_polls_until_terminal(server):
    base, state = server
    snap = _client(base).wait_scan("j-0001", timeout=5.0, poll_wait=0.1)
    assert snap["state"] == "done"
    assert state.scan_calls >= 2  # looped past the first (running) response


def test_wait_scan_timeout_raises(server):
    base, _ = server
    # deadline already exhausted -> TaviTimeout before any successful terminal state.
    with pytest.raises(TaviTimeout):
        _client(base).wait_scan("j-0001", timeout=0.0)


def test_retry_after_on_503(server):
    base, state = server
    # Exercise the retry path via the transport helper (the flaky endpoint 503s twice, then 200s).
    client = _client(base, max_retries=5)
    result = client._request("GET", "/flaky")
    assert result["ok"] is True
    assert state.flaky_calls == 3          # two 503s then success
    assert state.retry_after_hits == 2


def test_http_error_raises_with_body(server):
    base, _ = server
    with pytest.raises(TaviError) as exc:
        _client(base).get_scan_data("nope")
    assert exc.value.status == 404
    assert "unknown_job" in exc.value.body


def test_transport_error_raises(server):
    # Point at a closed port on localhost.
    client = TaviClient("http://127.0.0.1:1/api/v1", sleep=lambda s: None, timeout=1.0)
    with pytest.raises(TaviError):
        client.get_schema()
