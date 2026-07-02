"""The experiment workspace (UNIFICATION_DESIGN §1, §2.2) — one directory per experiment.

Layout:

    <workspace>/
      card.policy.toml    # human-owned; the machine NEVER writes it (Phase 2 D8)
      card.state.json     # machine-owned accumulated state (calibration, prior, exclusions)
      session.json        # machine bookkeeping (manifest + ledger); deletable/rebuildable
      report/             # report.md/.html/.json, reduction.*, results.csv, figures/

`Workspace` is a thin path holder + load/save. `load()` returns the in-memory `(Card, SessionState)`
pair; `save()` writes both machine-owned files atomically. The policy TOML is only ever read.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from isar.schema.card import Card
from isar.session.state import SessionState

POLICY_NAME = "card.policy.toml"
STATE_NAME = "card.state.json"
SESSION_NAME = "session.json"
REPORT_DIRNAME = "report"


@dataclass
class Workspace:
    root: Path

    @classmethod
    def open(cls, root) -> "Workspace":
        return cls(Path(root))

    # ---- paths -------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return self.root / POLICY_NAME

    @property
    def state_path(self) -> Path:
        return self.root / STATE_NAME

    @property
    def session_path(self) -> Path:
        return self.root / SESSION_NAME

    @property
    def report_dir(self) -> Path:
        return self.root / REPORT_DIRNAME

    def exists(self) -> bool:
        return self.policy_path.exists()

    # ---- load / save -------------------------------------------------------
    def load(self) -> tuple[Card, SessionState]:
        if not self.policy_path.exists():
            raise FileNotFoundError(
                f"no {POLICY_NAME} in workspace {self.root} — run `isar init` first"
            )
        card = Card.load(self.policy_path, self.state_path)
        session = SessionState.load(self.session_path)
        return card, session

    def save(self, card: Card, session: SessionState) -> None:
        """Persist the two machine-owned files. Never touches card.policy.toml (D8)."""
        self.root.mkdir(parents=True, exist_ok=True)
        card.save_state(self.state_path)        # atomic within schema.card
        session.save(self.session_path)         # atomic (write-temp + os.replace)
