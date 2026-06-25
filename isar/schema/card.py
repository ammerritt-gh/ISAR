"""The experiment card (§6, D8) — per-experiment policy + accumulated state, in two files.

`<exp>.policy.toml` (human-authored, comment-bearing, **read-only to the machine**) holds every
per-experiment decision the old parsers hardcoded: the per-analyzer detector registry, the
energy/normalization policy, the column names, the snap/exclusion rules. `<exp>.state.json`
(machine-written) holds `exclusions` + `corrections`, appended by reduction and reviewed by the
human. One in-memory `Card` = `CardPolicy` + `CardState`, two loaders; `save_state` writes JSON
only — the machine never opens the policy TOML for writing (D8 ownership, which makes the
correction loop §8 safe).

The card schema is its own test: if a parser ever needs a literal this can't express, the card is
wrong — fix the card, not the parser (§6).
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from isar.schema import _json
from isar.schema.scan import COUNTING_MODES, ENERGY_STATUSES, RAW_ABSCISSA_KINDS

SCHEMA_VERSION = "isar.card/1"
NORMALIZE_BY = ("monitor", "time", "none")


@dataclass
class DetectorSpec:
    """One analyzer/detector channel in the per-experiment registry (§6, §0.3)."""
    detector_id: str                 # "ana1".."ana9" (S30) | "det0" (IN8)
    value_index: int                 # which #V line holds this analyzer's H K L (S30: 4..12)
    signal_column: str               # resolved by NAME from #L (e.g. "Ana1_3std")
    enabled: bool = True             # does this analyzer carry signal THIS experiment (§0.3)
    q_offset: list[float] | None = None  # FALLBACK only (D2); None when the file logs per-analyzer Q


@dataclass
class CardPolicy:
    facility: str
    instrument: str
    format_version: str
    plugin: str
    detectors: list[DetectorSpec]
    energy_column: str
    monitor_column: str
    counting_mode: str               # "monitor" | "time"
    normalize_by: str                # "monitor" | "time" | "none" — which column divides the signal
    raw_abscissa_kind: str
    energy_status: str
    measurement_pattern: str
    alignment_pattern: str
    temperature_rule: dict
    collation: dict
    time_column: str | None = None
    monitor_reference: float | None = None
    variance_convention: str = "none"  # D3
    min_points: int = 5
    hkl_round_step: float = 0.05
    exclusions_seed: list = field(default_factory=list)
    # Raw-S30 ingestion (optional; absent in the supplementary card, which keeps these defaults).
    multiword_columns: list = field(default_factory=list)   # #L names containing spaces
    miniscans_per_point: int = 0                             # raw fscan group size (0 = no grouping)
    reduction: dict = field(default_factory=dict)            # cosmic-ray rejection policy (§7.3)
    ascan_classification: dict = field(default_factory=dict)  # motor -> scan_type (default alignment)
    qc: dict = field(default_factory=dict)                   # deterministic QC thresholds (§7.1-7.2)


@dataclass
class CardState:
    exclusions: list = field(default_factory=list)
    corrections: list = field(default_factory=list)  # append-only audit (LLM vs human, §8)

    def to_dict(self) -> dict:
        return _json.to_jsonable(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CardState":
        return _json.from_dataclass(cls, d)


def load_policy(path) -> CardPolicy:
    with open(path, "rb") as f:
        d = tomllib.load(f)
    return _json.from_dataclass(CardPolicy, d)


def load_state(path) -> CardState:
    p = Path(path)
    if not p.exists():
        return CardState()
    return CardState.from_dict(json.loads(p.read_text(encoding="utf-8")))


@dataclass
class Card:
    policy: CardPolicy
    state: CardState

    @classmethod
    def load(cls, policy_path, state_path=None) -> "Card":
        policy = load_policy(policy_path)
        state = load_state(state_path) if state_path is not None else CardState()
        return cls(policy, state)

    def save_state(self, path) -> Path:
        """Write the machine-owned state half as JSON. Never touches the policy TOML (D8)."""
        p = Path(path)
        p.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        return p

    def validate(self) -> list[str]:
        """Static card checks (value-vs-file checks happen at parse time)."""
        errs: list[str] = []
        pol = self.policy
        if pol.energy_status not in ENERGY_STATUSES:
            errs.append(f"energy_status {pol.energy_status!r} not in {ENERGY_STATUSES}")
        if pol.counting_mode not in COUNTING_MODES:
            errs.append(f"counting_mode {pol.counting_mode!r} not in {COUNTING_MODES}")
        if pol.normalize_by not in NORMALIZE_BY:
            errs.append(f"normalize_by {pol.normalize_by!r} not in {NORMALIZE_BY}")
        if pol.raw_abscissa_kind not in RAW_ABSCISSA_KINDS:
            errs.append(f"raw_abscissa_kind {pol.raw_abscissa_kind!r} not in {RAW_ABSCISSA_KINDS}")
        seen = set()
        for det in pol.detectors:
            if det.detector_id in seen:
                errs.append(f"duplicate detector_id {det.detector_id!r}")
            seen.add(det.detector_id)
            if det.value_index < 0:
                errs.append(f"detector {det.detector_id!r} value_index {det.value_index} < 0")
            if not det.signal_column:
                errs.append(f"detector {det.detector_id!r} has empty signal_column")
        return errs
