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

A plugin in `SHIPPED_FORMATS` (BL43) takes its format knowledge — file patterns, column families,
detectors, calibration — from the card file shipped with ISAR at every load, never from the workspace
copy; the workspace card keeps only the analysis settings (`_with_shipped_format`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tomllib
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from isar.schema import _json
from isar.schema.analyzer_layout import layout_problems
from isar.schema.hypotheses import resolve_hypothesis
from isar.schema.scan import COUNTING_MODES, ENERGY_STATUSES, RAW_ABSCISSA_KINDS

log = logging.getLogger(__name__)

SCHEMA_VERSION = "isar.card/1"
NORMALIZE_BY = ("monitor", "time", "none")

# Instruments whose format knowledge is read from the file shipped with ISAR, never from the workspace card.
SHIPPED_FORMATS: dict[str, Path] = {
    "sp8_bl43": Path(__file__).resolve().parents[1] / "parsers" / "cards" / "bl43lxu.policy.toml",
}
# CardPolicy fields that are format knowledge: for a SHIPPED_FORMATS plugin they come from the shipped file.
FORMAT_KEYS: frozenset[str] = frozenset({
    "facility", "instrument", "format_version", "plugin", "detectors",
    "energy_column", "monitor_column", "time_column", "counting_mode", "normalize_by",
    "raw_abscissa_kind", "energy_status", "variance_convention",
    "measurement_pattern", "alignment_pattern",
    "files", "reader", "calibration", "facts", "analyzer_layouts",
    "multiword_columns", "miniscans_per_point", "ascan_classification",
})


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
    quasielastic_fwhm: float | None = None  # broad QE Lorentzian FWHM (meV) for the §8 TAS
    #                                         structure component; None ⇒ no QE hypothesis (IXS default)
    # The largest energy transfer this instrument configuration can reach (meV): the kinematic
    # ceiling of the spectrometer, an INSTRUMENT fact and the top of the energy domain every
    # targeted campaign plans in (owner ruling 2026-09-05: no phonon-specific search ranges — the
    # domain is the instrument above and the elastic line below). Absent ⇒ the drive assumes the
    # shipped default and says so in the log.
    max_energy_transfer_meV: float | None = None
    dispersion: dict = field(default_factory=dict)  # steering branch-assignment geometry:
    #   {"zone_period": float}  — reduced-zone fold period (rlu) in which the acoustic branch is
    #   gapless (ω₀→0 at a zone centre). Absent ⇒ the branch-assignment posterior is off and the
    #   session falls back to the pre-steering polyfit prior. The Al_phonon_DFT MVP card sets 2.0.
    exclusions_seed: list = field(default_factory=list)
    # Raw-S30 ingestion (optional; absent in the supplementary card, which keeps these defaults).
    multiword_columns: list = field(default_factory=list)   # #L names containing spaces
    miniscans_per_point: int = 0                             # raw fscan group size (0 = no grouping)
    reduction: dict = field(default_factory=dict)            # cosmic-ray rejection policy (§7.3)
    ascan_classification: dict = field(default_factory=dict)  # motor -> scan_type (default alignment)
    qc: dict = field(default_factory=dict)                   # deterministic QC thresholds (§7.1-7.2)
    adjudication: dict = field(default_factory=dict)         # LLM adjudication config (§7.3)
    # Unification (the retired unification plan §2.1, git history; current contract: docs/WORKSPACE_AND_CLI.md) — the CLI/session layer reads these. Absent in older
    # cards; defaults preserve pre-unification behavior. Without these fields declared here,
    # `_json.from_dataclass` would silently DROP the [calibration]/[fitting]/[sample] TOML tables.
    calibration: dict = field(default_factory=dict)          # [calibration] — method + knobs (§3)
    fitting: dict = field(default_factory=dict)              # [fitting] — user guidelines (§6)
    sample: dict = field(default_factory=dict)               # [sample] — name override, require_alignment
    hypotheses: dict = field(default_factory=dict)           # [hypotheses.*] — human physics intent
    # Per-detector file sets (opt-in; absent ⇒ ingest is one file, one parse). Opaque tables read by
    # their consumer; only [files] is checked here.
    #   [files]  measurement = regexes (re.fullmatch on the file NAME) with named groups `set` (the
    #            scan's key) and `analyzer`; sidecar = names with a `{set}` placeholder; ignore = globs.
    files: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)                # [facts] — card-declared fact values (lowest precedence);
    #                                                          # BL43 reads none: its values are entered in ISAR
    reader: dict = field(default_factory=dict)               # [reader] — plugin options
    analyzer_layouts: list = field(default_factory=list)     # [[analyzer_layouts]] — where each analyzer sits (analyzer_layout.py)
    # Inline human-authored distinction policies.  Authoring markers are materialized into exact
    # campaign scope fingerprints when an objective is frozen; code never writes this table.


UNKNOWN_KEY = "unknown top-level key"       # the start of each unknown-key warning (the rest are notes)


@dataclass(frozen=True)
class PolicyValidation:
    """Result of validating human-authored ``card.policy.toml`` text.

    This deliberately lives beside the schema rather than in the Qt editor: command-line and
    notebook consumers get the same construction and semantic checks, while the GUI can present
    unknown keys as a deliberate warning instead of silently dropping them.
    """

    policy: CardPolicy | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        """The warnings about keys the card schema does not have."""
        return tuple(w for w in self.warnings if w.startswith(UNKNOWN_KEY))

    @property
    def notes(self) -> tuple[str, ...]:
        """The other warnings: keys ISAR reads from elsewhere (the shipped BL43 file, values entered in ISAR)."""
        return tuple(w for w in self.warnings if not w.startswith(UNKNOWN_KEY))


def _with_shipped_format(data: dict) -> tuple[dict, list[str]]:
    """``(merged, notes)``: for a `SHIPPED_FORMATS` plugin the shipped file's dict, with the workspace's
    non-format keys on top (tables replaced whole); every workspace format key is dropped, and a note says
    so for each one that differs from the shipped value. Any other plugin: ``(data, [])`` unchanged.
    Read at every call (no cache), so a corrected shipped file reaches the next load. Never writes."""
    plugin = data.get("plugin")
    path = SHIPPED_FORMATS.get(plugin) if isinstance(plugin, str) else None
    if path is None:
        return data, []
    with open(path, "rb") as f:
        shipped = tomllib.load(f)
    merged = dict(shipped)
    notes: list[str] = []
    for key, value in data.items():
        if key not in FORMAT_KEYS:
            merged[key] = value
        elif key not in shipped:
            why = " (values are entered in ISAR, not in the card)" if key == "facts" else ""
            notes.append(f"{key!r} in the workspace card is not used: {plugin} does not read it{why}")
        elif value != shipped[key]:
            extra = (" (the resolution folder is a setting chosen in ISAR)"
                     if key == "calibration" and isinstance(value, dict) and "resolution_folder" in value else "")
            notes.append(f"{key!r} in the workspace card is not used: it is read from the shipped "
                         f"{path.name}{extra}")
    return merged, notes


def validate_policy_text(text: str) -> PolicyValidation:
    """Parse and validate a policy TOML document without writing anything.

    Syntax errors, dataclass construction failures, and :meth:`Card.validate` findings are
    blocking errors.  Unknown *top-level* keys are warnings because existing cards may carry
    forward-compatible annotations, but the editor must make their otherwise-silent omission
    explicit before a scientist saves.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return PolicyValidation(None, (f"TOML error: {exc}",))
    if not isinstance(data, dict):  # defensive: tomllib currently always returns a dict
        return PolicyValidation(None, ("TOML document must contain a table",))

    known = {f.name for f in fields(CardPolicy)}
    warnings = tuple(f"{UNKNOWN_KEY} {key!r}" for key in sorted(data) if key not in known)
    try:
        merged, notes = _with_shipped_format(data)
    except Exception as exc:  # a broken install must be a visible error, never a GUI crash
        return PolicyValidation(None, (f"shipped format file error: {exc}",), warnings)
    warnings += tuple(notes)
    try:
        policy = _json.from_dataclass(CardPolicy, merged)
    except (TypeError, ValueError, KeyError) as exc:
        return PolicyValidation(None, (f"card construction error: {exc}",), warnings)
    except Exception as exc:  # malformed human input must be reported, never crash the editor
        return PolicyValidation(None, (f"card construction error: {exc}",), warnings)

    try:
        errors = tuple(Card(policy, CardState()).validate())
    except Exception as exc:  # a malformed policy must be a visible validation error, never a GUI crash
        return PolicyValidation(policy, (f"card validation error: {exc}",), warnings)
    return PolicyValidation(policy, errors, warnings)


def _files_errors(files) -> list[str]:
    """Static checks of the `[files]` table: every measurement regex compiles and names the
    `analyzer` and `set` groups ingest groups by; sidecar templates carry `{set}`."""
    if not isinstance(files, dict):
        return ["files must be a table"]
    errs: list[str] = []
    for key in ("measurement", "sidecar", "ignore"):
        val = files.get(key, [])
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            errs.append(f"files.{key} must be a list of strings")
    for key in sorted(set(files) - {"measurement", "sidecar", "ignore"}):
        errs.append(f"files.{key}: unknown key (expected measurement, sidecar, ignore)")
    if errs:
        return errs
    measurement = files.get("measurement", [])
    if not measurement and (files.get("sidecar") or files.get("ignore")):
        errs.append("files.measurement is required when sidecar or ignore is given")
    for pattern in measurement:
        try:
            groups = re.compile(pattern).groupindex
        except re.error as exc:
            errs.append(f"files.measurement {pattern!r} does not compile: {exc}")
            continue
        for needed in ("analyzer", "set"):
            if needed not in groups:
                errs.append(f"files.measurement {pattern!r} must name a (?P<{needed}>...) group")
    for template in files.get("sidecar", []):
        if "{set}" not in template:
            errs.append(f"files.sidecar {template!r} must contain the {{set}} placeholder")
    return errs


def _layout_errors(layouts, detector_ids) -> list[str]:
    """Static checks of the `[[analyzer_layouts]]` tables: each has the shape the resolver reads, its analyzers are
    detectors of this card, and the ids are unique."""
    if not isinstance(layouts, list):
        return ["analyzer_layouts must be a list of tables"]
    errs: list[str] = []
    seen: set[str] = set()
    for entry in layouts:
        lid = entry.get("id") if isinstance(entry, dict) else None
        errs.extend(f"analyzer_layouts {lid!r}: {p}" for p in layout_problems(entry))
        if isinstance(lid, str):
            if lid in seen:
                errs.append(f"duplicate analyzer_layouts id {lid!r}")
            seen.add(lid)
        rows = entry.get("rows") if isinstance(entry, dict) else None
        for row in rows if isinstance(rows, list) else ():
            for name in row if isinstance(row, list) else ():
                if isinstance(name, str) and name not in detector_ids:
                    errs.append(f"analyzer_layouts {lid!r}: {name!r} is not a detector of this card")
    return errs


@dataclass
class CardState:
    exclusions: list = field(default_factory=list)
    corrections: list = field(default_factory=list)  # append-only audit (LLM vs human, §8)
    # Unification (the retired unification plan §2.1, git history; current contract: docs/WORKSPACE_AND_CLI.md) — the docs/ARCHITECTURE.md §6 accumulated-state half. All optional
    # with list defaults, so a pre-unification `<exp>.state.json` (exclusions + corrections only)
    # loads unchanged.
    resolution_versions: list = field(default_factory=list)  # [{version, method, source, created, ...}]
    energy_zero_series: list = field(default_factory=list)   # [{key, e0, source_scan}]
    tabulated_calibration: dict = field(default_factory=dict)  # "exp:scan" -> {setting, source, deviances,
    #   flags, detectors: {det: {zero, zero_source, fit, flags, held_reason}}}; method tabulated_elastic
    #   (isar.session.calibrations). Updated in place per new or changed scan, so it grows linearly.
    dispersion_prior: list = field(default_factory=list)     # PriorPoint dicts (§5)
    sample_map: list = field(default_factory=list)           # [{from_scan, sample, source}]
    branch_models: list = field(default_factory=list)        # BranchModel dicts (steering, Phase 2+)
    branch_belief: list = field(default_factory=list)        # flat per-Q rows for viewer/report contract
    branch_belief_models: list = field(default_factory=list) # regression ingredients for drive scoring
    branch_tracks: dict = field(default_factory=dict)        # joint path assignment + connected segments
    campaign_target: dict = field(default_factory=dict)      # active frozen objective branch/search
    hypothesis_snapshot: dict = field(default_factory=dict)  # resolved set + source + content fingerprint
    hypothesis_mismatch: dict = field(default_factory=dict)  # persisted prior-vs-data PPC warning (O11)
    acquisition_provenance: list = field(default_factory=list)  # append-only TAVI job→logical scan map
    #   {sample, t_bin, q, hkl, omega0, sigma, entropy, resolvable} — the drive reads this (durable,
    #   boundary-safe) to steer ω(q) by the assignment-inflated σ instead of raw best_omega0 (Phase A).
    lattice_epochs: list = field(default_factory=list)       # versioned lattice/orientation interpretation
    #   (isar.geometry.epochs). A lattice/orientation is not static policy: it can change mid-experiment
    #   (phase transition, realignment, HK0↔H0L swap) and be corrected retroactively. Scans stay in
    #   absolute instrument coords; each epoch maps absolute Q→HKL, and grouping keys on the derived HKL.
    #   Append-only: correct by adding a new epoch (with `supersedes`), never by editing scans. Record:
    #   {version:int, created:iso8601, valid_from:{scan_ref:str|None, ts:str|None}, supersedes:int|None,
    #    lattice:{a,b,c,alpha,beta,gamma}, orient:{u:[h,k,l], v:[h,k,l]}, method:str, source:str}.
    #   Resolve with isar.geometry.resolve_epoch; build the sample with sample_state_from_epoch.

    def to_dict(self) -> dict:
        return _json.to_jsonable(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CardState":
        return _json.from_dataclass(cls, d)


def load_policy(path) -> CardPolicy:
    with open(path, "rb") as f:
        d = tomllib.load(f)
    d, notes = _with_shipped_format(d)      # an unreadable shipped file raises: a broken install
    for note in notes:
        log.warning("%s: %s", path, note)
    return _json.from_dataclass(CardPolicy, d)


def unknown_toml_tables(path) -> list[str]:
    """Top-level TOML keys that match no `CardPolicy` field (the retired unification plan §7, git history; current contract: docs/WORKSPACE_AND_CLI.md).

    `_json.from_dataclass` silently drops unknown keys, so a typo'd section (`[fittng]`) or a
    misspelled scalar vanishes without error. The CLI calls this and warns, turning a silent
    no-op into a visible one. Purely diagnostic — never raises, never mutates.
    """
    with open(path, "rb") as f:
        d = tomllib.load(f)
    known = {f.name for f in fields(CardPolicy)}
    return sorted(k for k in d if k not in known)


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
        """Write the machine-owned state half as JSON, **atomically**. Never touches the policy TOML
        (D8). Atomic (write-temp + ``os.replace``) so a crash mid-write cannot leave a truncated /
        corrupt ``card.state.json`` — the drive reads this file across process boundaries to recover the
        branch model (and the rest of the accumulated state) after a crash, so a torn write would poison
        the restart. On failure the prior file is left intact."""
        p = Path(path)
        text = json.dumps(self.state.to_dict(), indent=2)
        tmp = p.with_name(p.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)      # atomic on the same filesystem
        except BaseException:
            try:                    # best-effort: don't leave a stale .tmp fragment behind
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
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
        if not pol.detectors:
            errs.append("at least one detector is required")
        seen: set[str] = set()
        for det in pol.detectors:
            detector_id = det.detector_id
            if not isinstance(detector_id, str) or not detector_id:
                errs.append("detector has a non-string or empty detector_id")
            elif detector_id in seen:
                errs.append(f"duplicate detector_id {detector_id!r}")
            else:
                seen.add(detector_id)

            value_index = det.value_index
            if not isinstance(value_index, int) or isinstance(value_index, bool):
                errs.append(f"detector {detector_id!r} value_index must be an integer")
            elif value_index < 0:
                errs.append(f"detector {detector_id!r} value_index {value_index} < 0")

            if not isinstance(det.signal_column, str) or not det.signal_column:
                errs.append(f"detector {detector_id!r} has a non-string or empty signal_column")
        if pol.files:
            errs.extend(_files_errors(pol.files))
        errs.extend(_layout_errors(pol.analyzer_layouts,
                                   {d.detector_id for d in pol.detectors if isinstance(d.detector_id, str)}))
        for table in ("facts", "reader"):
            if not isinstance(getattr(pol, table), dict):
                errs.append(f"{table} must be a table")
        if not isinstance(pol.hypotheses, dict):
            errs.append("hypotheses must be a table of named hypothesis tables")
        else:
            for name, declaration in sorted(pol.hypotheses.items()):
                if not isinstance(declaration, dict):
                    errs.append(f"hypotheses.{name} must be a table")
                    continue
                try:
                    resolve_hypothesis(str(name), declaration)
                except (TypeError, ValueError) as exc:
                    errs.append(f"hypotheses.{name}: {exc}")
        return errs
