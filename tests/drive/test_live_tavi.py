"""Live integration: the whole plumbing chain against a running TAVI (marker `live`).

Skipped unless ``ISAR_LIVE_TAVI`` is set (TAVI must be up at 127.0.0.1:8642). Exercises the full
non-analysis path the driver loop will use: schema → submit a tiny deterministic scan → wait →
get data → save the scan JSON into an ISAR workspace → `isar run` (subprocess) → read results.csv.

No physics is asserted — this is a plumbing smoke test. It reports the ISAR verdict it got, but only
asserts the chain completes and produces rows.

The background tests are the one exception to "no physics": paired NOISELESS scans make the planted
background an exact equality (background-on minus background-off == the resolved rate × monitor), so
they check that TAVI plants what was requested, that the delivery source is reported honestly, and
that ISAR's record keeps the fingerprint identity while the numbers stay behind in the raw JSON.
"""
import json
import os
from pathlib import Path

import pytest

from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import run_isar
from isar.drive.isar_io.scan_writer import save_scan_json

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ISAR_LIVE_TAVI"),
                       reason="set ISAR_LIVE_TAVI (and run TAVI at 127.0.0.1:8642) to enable"),
]


def _init_tavi_workspace(root: Path) -> Path:
    from isar.parsers import tavi as tavi_mod
    from isar.session.state import SessionState
    root.mkdir(parents=True, exist_ok=True)
    (root / "card.policy.toml").write_text(
        Path(tavi_mod._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    (root / "card.state.json").write_text("{}", encoding="utf-8")
    SessionState().save(root / "session.json")
    return root


def test_live_end_to_end_plumbing(tmp_path):
    client = TaviClient()

    schema = client.get_schema()
    engines = schema.get("engines", [])
    if "deterministic" not in engines:
        pytest.skip(f"TAVI does not advertise the deterministic engine (engines={engines})")

    body = {
        "parameters": {"scan_command1": "deltaE 0.5 3.0 0.625", "number_neutrons": 1000000},
        "engine": "deterministic",
        "seed": 1,
        "isolated": True,
    }
    submit = client.submit_scan(body)
    job_id = submit["job_id"]

    snap = client.wait_scan(job_id, timeout=120.0, poll_wait=10.0)
    state = snap.get("state")
    if state != "done":
        pytest.skip(f"scan {job_id} terminated {state!r} (error={snap.get('error')}) — "
                    "likely no analytic ground truth for the current sample; plumbing chain not exercised")

    data = client.get_scan_data(job_id)
    assert data["job_id"] == job_id
    assert "result" in data

    ws = _init_tavi_workspace(tmp_path / "ws")
    data_dir = ws / "data"
    saved = save_scan_json(data, data_dir)
    assert saved.exists()

    proc = run_isar(ws, [data_dir])
    assert proc.returncode == 0

    rows = read_results_csv(ws)
    assert rows, "isar run produced no results.csv rows"
    state_dict = read_card_state(ws)

    verdicts = [r.get("verdict") for r in rows if r.get("verdict")]
    print(f"[live] job {job_id}: {len(rows)} result row(s); verdicts={verdicts}; "
          f"dispersion_prior points={len(state_dict.get('dispersion_prior', []))}")


def test_live_short_campaign_reaches_met(tmp_path):
    """A short end-to-end driver campaign on the deterministic tier via the LLM-free --spec path.

    Tolerance 0.5 meV (achievable per the calibration findings), H 2.1→2.4 step 0.1 (4 cheap points),
    a generous budget. Asserts the campaign terminates ``met`` with a session log showing the
    propose → analyze → decide chain.
    """
    from isar.drive.loop import run_campaign
    from isar.drive.objective import ObjectiveSpec
    from isar.drive.session_log import read_log

    client = TaviClient()
    schema = client.get_schema()
    if "deterministic" not in schema.get("engines", []):
        pytest.skip("TAVI does not advertise the deterministic engine")

    ws = _init_tavi_workspace(tmp_path / "ws")
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e13}, tier="deterministic", name="live-mvp")

    summary = run_campaign(objective, {"workspace": ws, "seed": 11, "max_iters": 80})
    kinds = {e["kind"] for e in read_log(summary["session_log"])}
    print(f"[live] campaign stop={summary['stop']['state']} reasons={summary['stop']['reasons']}")
    for expected in ("propose", "analyze", "decide", "stop"):
        assert expected in kinds, f"missing {expected} in session log"
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]


_NEUTRONS = 1000000


def _require_background(client) -> dict:
    """The instrument schema, skipping unless it advertises the deterministic engine + background."""
    schema = client.get_schema()
    engines = schema.get("engines", [])
    if "deterministic" not in engines:
        pytest.skip(f"TAVI does not advertise the deterministic engine (engines={engines})")
    if not isinstance(schema.get("background"), dict):
        pytest.skip("TAVI advertises no background block in GET /schema (pre-background build)")
    return schema


def _noiseless_scan(client, *, background=None) -> dict:
    """Submit the standard tiny NOISELESS deterministic scan and return its `/scan/{id}/data` body.

    Noiseless means the counts array holds the exact Poisson means, which is what makes the
    background-on minus background-off difference an exact equality rather than a statistical one.
    ``background`` (when given) rides as the per-scan override; omitted, TAVI uses its config default.
    """
    body = {
        "parameters": {"scan_command1": "deltaE 0.5 3.0 0.625", "number_neutrons": _NEUTRONS},
        "engine": "deterministic",
        "seed": 1,
        "isolated": True,
        "noiseless": True,
    }
    if background is not None:
        body["background"] = background
    job_id = client.submit_scan(body)["job_id"]
    snap = client.wait_scan(job_id, timeout=120.0, poll_wait=10.0)
    if snap.get("state") != "done":
        pytest.skip(f"scan {job_id} terminated {snap.get('state')!r} (error={snap.get('error')}) — "
                    "likely no analytic ground truth for the current sample")
    return client.get_scan_data(job_id)


def test_live_background_round_trip(tmp_path):
    """TAVI plants the configured background; ISAR records its identity and never its magnitude.

    Three noiseless scans of the same points: config default (background on), per-scan override off,
    per-scan override on. Off-vs-on differ by exactly the resolved flat rate × monitor — read from the
    metadata block itself, never hardcoded — and the ISAR parser turns the on-scan into a record that
    carries the effective fingerprint and none of the numbers.
    """
    from isar.parsers import tavi as tavi_mod

    client = TaviClient()
    _require_background(client)

    # Save the operator's profile FIRST, and check it is restorable BEFORE the try — nothing has been
    # mutated yet at this point, so a malformed GET /background fails without leaving the instrument
    # reconfigured. Everything after the try does mutate it, and the finally puts it back.
    saved = client.get_background()
    assert isinstance(saved.get("spec"), dict), \
        f"GET /background answered without a restorable 'spec' object: {sorted(saved)}"
    try:
        client.set_background({"enabled": True, "preset": "flat_high"})

        # 1. No per-scan key: the scan must report the instrument's configured profile as its source.
        default_data = _noiseless_scan(client)
        default_block = default_data["result"]["metadata"]["background"]
        assert default_block["enabled"] is True
        assert default_block["preset"] == "flat_high"
        assert default_block["source"] == "config_default"
        assert default_block["profile_fingerprint"]
        assert default_block["effective_fingerprint"]

        # 2. Paired exactness: the same scan with the background explicitly off, then explicitly on.
        off_data = _noiseless_scan(client, background={"enabled": False, "preset": "none"})
        on_data = _noiseless_scan(client, background={"enabled": True, "preset": "flat_high"})
        off_block = off_data["result"]["metadata"]["background"]
        on_block = on_data["result"]["metadata"]["background"]
        assert off_block["enabled"] is False
        assert off_block["source"] == "per_scan_override"
        assert on_block["enabled"] is True
        assert on_block["source"] == "per_scan_override"
        # Identity is delivery-independent: the same physics through a different door fingerprints the
        # same, so the override-on scan must match the config-default one.
        assert on_block["profile_fingerprint"] == default_block["profile_fingerprint"]
        assert on_block["effective_fingerprint"] == default_block["effective_fingerprint"]

        # The expected difference comes from the metadata's own resolved terms. flat_high is a single
        # flat instrument term, so the planted mean is monitor × rate at every point; the assertions
        # below pin that shape rather than assume it.
        terms = on_block["terms"]
        assert terms, "an enabled background stamped no resolved terms"
        assert all(t["shape"] == "flat" for t in terms), \
            f"this test's monitor × rate arithmetic only covers flat terms, got {terms}"
        assert all(t["origin"] != "sample" for t in terms), \
            "a sample-origin term would scale by diffuse_background, not by the monitor alone"
        expected = _NEUTRONS * sum(float(t["params"]["rate"]) for t in terms)
        assert expected > 0.0

        off_counts = off_data["result"]["counts"]
        on_counts = on_data["result"]["counts"]
        assert len(off_counts) == len(on_counts)
        compared = 0
        for i, (lo, hi) in enumerate(zip(off_counts, on_counts)):
            if lo is None or hi is None:      # skipped/infeasible point — nothing to compare
                continue
            assert float(hi) - float(lo) == pytest.approx(expected, rel=1e-9), \
                f"point {i}: background-on minus background-off is not the planted flat rate"
            compared += 1
        assert compared, "no comparable points — the paired scans were entirely skipped"

        # 3. Through the ISAR parser: identity survives, magnitude does not.
        saved_json = save_scan_json(on_data, tmp_path / "data")
        rec = tavi_mod.parse_file(saved_json, tavi_mod.card_template())[0]
        prov = rec.raw_header["background_provenance"]
        assert set(prov) == set(tavi_mod.BACKGROUND_SAFE_KEYS)
        assert prov["effective_fingerprint"] == on_block["effective_fingerprint"]
        assert prov["profile_fingerprint"] == on_block["profile_fingerprint"]
        assert prov["source"] == "per_scan_override"
        assert rec.raw_header["tavi"]["result"]["metadata"]["background"] == {
            "redacted": "sim_truth", "effective_fingerprint": on_block["effective_fingerprint"]}
        payload = json.dumps(rec.raw_header["tavi"]) + json.dumps(prov)
        for forbidden in ("flat_high", "terms", "parameter_units", "sample_scale"):
            assert forbidden not in payload, f"{forbidden!r} leaked onto the record"
        print(f"[live] background round trip: preset=flat_high, planted {expected:.3f} counts/point, "
              f"effective_fingerprint={on_block['effective_fingerprint']}")
    finally:
        client.set_background(saved["spec"])


def test_live_campaign_sample_background(tmp_path):
    """A campaign's declared background is frozen once and stamped on every scan it submits.

    Deliberately smaller than the MVP campaign above (two path points, tight iteration cap): the claim
    under test is the transport, not convergence, so no terminal state is asserted.
    """
    from isar.drive.loop import run_campaign
    from isar.drive.objective import ObjectiveSpec
    from isar.drive.session_log import read_log

    client = TaviClient()
    _require_background(client)

    ws = _init_tavi_workspace(tmp_path / "ws")
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e13}, tier="deterministic", name="live-background")

    summary = run_campaign(objective, {"workspace": ws, "seed": 11, "max_iters": 12,
                                       "sample_background": {"enabled": True, "preset": "flat_low"}})
    assert summary["stop"]["state"], "campaign produced no stop state"

    events = read_log(summary["session_log"])
    frozen = [(e.get("payload") or {}) for e in events
              if (e.get("payload") or {}).get("event") == "sample_background_frozen"]
    assert len(frozen) == 1, f"expected exactly one sample_background_frozen event, got {len(frozen)}"
    assert frozen[0]["preset"] == "flat_low"
    assert frozen[0]["frozen"]["enabled"] is True
    assert frozen[0]["frozen"]["terms"], "the frozen profile carries no numeric terms"

    # Every scan the campaign actually produced must carry that same frozen truth, delivered as a
    # per-scan override. The raw JSON on disk keeps the numerics (the record does not).
    scans = sorted((ws / "data").glob("*.json"))
    if not scans:
        pytest.skip(f"campaign stopped {summary['stop']['state']!r} before completing a scan "
                    f"({summary['stop']['reasons']}) — stamping not exercised")
    for path in scans:
        block = json.loads(path.read_text(encoding="utf-8"))["result"]["metadata"]["background"]
        assert block["enabled"] is True
        assert block["source"] == "per_scan_override"
        assert block["effective_fingerprint"]
    print(f"[live] campaign stop={summary['stop']['state']}; {len(scans)} scan(s) stamped with the "
          f"frozen flat_low background")


@pytest.mark.skipif(not os.environ.get("ISAR_LIVE_LLMM"),
                    reason="set ISAR_LIVE_LLMM (and run LLMM at 127.0.0.1:8766) to enable")
def test_live_goal_to_spec():
    """goal→ObjectiveSpec against a live LLMM (skipped unless ISAR_LIVE_LLMM is set)."""
    from isar.drive.supervisor.goal_to_spec import goal_to_spec
    spec = goal_to_spec("map the acoustic phonon dispersion from H=2.1 to H=2.4 at K=0 L=0, "
                        "resolve the energy to +/- 0.5 meV, overnight")
    assert spec.tolerance_meV == pytest.approx(0.5, abs=0.2)
    assert spec.path[0][0] == pytest.approx(2.1, abs=0.05)
    print(f"[live] goal->spec: {spec.to_dict()}")
