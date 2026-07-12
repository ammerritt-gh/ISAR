"""isar_gui.mime — the drag-and-drop payload contract for scans and groups (Phase E, Qt-free).

Two custom MIME types carry ISAR's draggable units so any future drop target (the waterfall/
comparison view, an analysis basket) can accept a scan or a whole group *unchanged*:

* :data:`MIME_SCAN_IDS` — a flat list of scan keys (scan-level ``"exp:scan"`` form). Both scan chips
  and table rows emit this, so every scan-consuming target only has to understand one type.
* :data:`MIME_GROUP_REF` — a reference to a group (manual or derived auto-group). A group header
  drag sets BOTH types — the group ref *and* the flattened member scan ids — so a scan-only drop
  target still works when handed a group.

Encoders/decoders are pure ``bytes`` <-> Python, utf-8 JSON, and **tolerant on decode**: garbage
never raises, it logs and returns the empty answer (``[]`` / ``None``). This module is imported by
Qt widgets but imports no Qt itself, so the contract is unit-tested directly.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

log = logging.getLogger(__name__)

MIME_SCAN_IDS = "application/x-isar-scan-ids"
MIME_GROUP_REF = "application/x-isar-group-ref"


def encode_scan_ids(keys: Iterable[str]) -> bytes:
    """Serialize an iterable of scan keys to a utf-8 JSON list payload."""
    return json.dumps([str(k) for k in keys]).encode("utf-8")


def decode_scan_ids(data) -> list[str]:
    """Parse a :data:`MIME_SCAN_IDS` payload to a list of scan keys; ``[]`` on anything malformed."""
    try:
        raw = bytes(data).decode("utf-8")
        parsed = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        log.debug("decode_scan_ids: ignoring malformed payload (%s)", exc)
        return []
    if not isinstance(parsed, list):
        log.debug("decode_scan_ids: payload is not a list (%r)", type(parsed).__name__)
        return []
    return [str(x) for x in parsed]


def encode_group_ref(kind: str, group_id: str, label: str) -> bytes:
    """Serialize a group reference (``kind`` = ``"manual"`` | ``"auto"``) to a utf-8 JSON payload."""
    return json.dumps({"kind": str(kind), "group_id": str(group_id),
                       "label": str(label)}).encode("utf-8")


def decode_group_ref(data) -> dict | None:
    """Parse a :data:`MIME_GROUP_REF` payload to its dict; ``None`` on anything malformed."""
    try:
        raw = bytes(data).decode("utf-8")
        parsed = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        log.debug("decode_group_ref: ignoring malformed payload (%s)", exc)
        return None
    if not isinstance(parsed, dict) or "group_id" not in parsed:
        log.debug("decode_group_ref: payload is not a group ref (%r)", parsed)
        return None
    return {"kind": str(parsed.get("kind") or ""),
            "group_id": str(parsed.get("group_id")),
            "label": str(parsed.get("label") or "")}
