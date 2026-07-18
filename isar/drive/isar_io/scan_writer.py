"""Save a finished TAVI scan into the ISAR workspace data dir (WORKING — LLM_HARNESS_DESIGN §4).

The driver saves the body of TAVI's ``GET /scan/{id}/data`` **verbatim** as ``{job_id}.json`` in the
workspace's data directory. ISAR's ``tavi`` parser plugin sniffs a TAVI scan by the top-level
``job_id`` + ``result`` keys (``isar/parsers/tavi.py`` ``matches``), then reads ``launch.parameters``
and ``result.counts`` — so the correct thing is to write the payload unchanged. Do NOT transform it:
any reshaping here would duplicate (and could drift from) the plugin's contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def scan_json_path(job_id: str, data_dir: str | Path) -> Path:
    """Return the sanitized workspace path used for one TAVI job identifier."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(job_id)) or "scan"
    return Path(data_dir) / f"{safe}.json"


def save_scan_json(data_dict: dict[str, Any], data_dir: str | Path) -> Path:
    """Write ``data_dict`` (a ``GET /scan/{id}/data`` body) verbatim as ``{job_id}.json``.

    ``data_dir`` is created if needed. Returns the path written. Requires the top-level ``job_id`` and
    ``result`` keys the ``tavi`` plugin sniffs — missing either is a programming error (a snapshot with
    no arrays came from ``GET /scan/{id}`` instead of ``.../data``), so raise rather than write junk
    ISAR would silently skip.
    """
    if not isinstance(data_dict, dict) or "job_id" not in data_dict or "result" not in data_dict:
        raise ValueError(
            "save_scan_json expects a GET /scan/{id}/data body with top-level 'job_id' and 'result' "
            "(the keys ISAR's tavi plugin sniffs); got keys "
            f"{sorted(data_dict) if isinstance(data_dict, dict) else type(data_dict).__name__}")
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = scan_json_path(str(data_dict["job_id"]), d)
    path.write_text(json.dumps(data_dict), encoding="utf-8")
    return path
