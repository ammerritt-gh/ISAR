"""PR #30 review response, W1: ``scene_hook.build_and_store`` must pass the card's OWN declared
``collation.t_round`` into ``build_scene``, not silently fall back to ``build_scene``'s 0.1
default (S30-legacy, not Stage II's).

Two claims: the wiring itself (the card's value reaches ``build_scene`` verbatim), and the
consequence that actually matters (two arenas a few kelvin apart share a partition under the
card's declared 5 K bin and do not under a much narrower one) — the wiring test alone could pass
on a build_scene that ignores the argument in a way that happens not to matter for one card.
"""
from isar.scene import build as scene_build
from isar.session import scene_hook
from isar.workspace_api import Workspace
from synthetic_ws import _POLICY_TOML, FULL_COLUMNS, stamp_ws

from .test_workspace_api_scene import _candidate, _row, _write_measurements

COLUMNS = FULL_COLUMNS + ["energy_lo", "energy_hi", "energy_bands", "evidence_sink",
                          "effective_exposure", "fit_selection_semantics"]

_POLICY_T_ROUND_0_1 = _POLICY_TOML.replace("t_round = 5.0", "t_round = 0.1")


def _ws_two_arenas(root, *, policy_toml, t_a=99.0, t_b=102.0):
    """Two same-q, same-candidate arenas at ``t_a``/``t_b`` kelvin, ready for the real adapter."""
    cands = [_candidate("phonon+bg")]
    rows = [_row(1, candidates=cands, T=t_a), _row(2, candidates=cands, T=t_b)]
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, rows=rows, columns=COLUMNS, policy_toml=policy_toml)
    _write_measurements(root, rows)
    return root


def test_build_and_store_passes_the_cards_declared_t_round(tmp_path, monkeypatch):
    """The wiring itself: whatever ``card.policy.collation.t_round`` the card declares is what
    ``build_scene`` is called with — never the function's own 0.1 default."""
    root = _ws_two_arenas(tmp_path, policy_toml=_POLICY_T_ROUND_0_1)
    captured = {}
    real_build_scene = scene_build.build_scene

    def spy(*a, **kw):
        captured["condition_t_round"] = kw.get("condition_t_round")
        return real_build_scene(*a, **kw)

    monkeypatch.setattr(scene_build, "build_scene", spy)
    scene_hook.build_and_store(root)
    assert captured["condition_t_round"] == 0.1


def test_build_and_store_defaults_to_stage_iis_five_kelvin_rule_when_the_card_is_silent(
        tmp_path, monkeypatch):
    """A card with no ``[collation] t_round`` key at all still reaches ``build_scene`` at Stage
    II's own default (5.0), never ``build_scene``'s bare 0.1."""
    policy_no_t_round = "\n".join(
        line for line in _POLICY_TOML.splitlines() if not line.strip().startswith("t_round"))
    root = _ws_two_arenas(tmp_path, policy_toml=policy_no_t_round)
    captured = {}
    real_build_scene = scene_build.build_scene

    def spy(*a, **kw):
        captured["condition_t_round"] = kw.get("condition_t_round")
        return real_build_scene(*a, **kw)

    monkeypatch.setattr(scene_build, "build_scene", spy)
    scene_hook.build_and_store(root)
    assert captured["condition_t_round"] == 5.0


def test_two_arenas_a_few_kelvin_apart_couple_under_the_cards_wide_bin(tmp_path):
    """The consequence: at the card's declared 5 K bin, 99 K and 102 K arenas (both round to the
    same 100 K bin) share a partition. Same q, same candidate labels, so their footprints are the
    only thing that can tell the partitions apart."""
    root = _ws_two_arenas(tmp_path, policy_toml=_POLICY_TOML)  # t_round = 5.0
    scene_hook.build_and_store(root)
    ws = Workspace.open(root)
    from isar.scene import store
    _pointer, payload = store.read_current(ws.root / "report")
    footprints = {f["scan_ref"]: f for f in payload["footprints"]}
    ids = {fp["partition_id"] for fp in footprints.values()}
    assert len(ids) == 1, footprints


def test_the_same_two_arenas_separate_under_a_narrow_bin(tmp_path):
    """Contrast case: the identical pair of arenas, only the card's declared bin changes (0.1 K),
    lands them in two separate partitions."""
    root = _ws_two_arenas(tmp_path, policy_toml=_POLICY_T_ROUND_0_1)
    scene_hook.build_and_store(root)
    ws = Workspace.open(root)
    from isar.scene import store
    _pointer, payload = store.read_current(ws.root / "report")
    footprints = {f["scan_ref"]: f for f in payload["footprints"]}
    ids = {fp["partition_id"] for fp in footprints.values()}
    assert len(ids) == 2, footprints
