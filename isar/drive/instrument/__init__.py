"""The instrument control surface client (LLM_HARNESS_DESIGN §2.3).

:mod:`tavi_client` is a thin stdlib-``urllib`` wrapper over TAVI's REST API (base
``http://127.0.0.1:8642/api/v1``): learn the fields (``GET /schema``), confirm writes are allowed
(``GET /state``), dry-run feasibility (``POST /validate``), get an advisory step width
(``GET /resolution``), submit an isolated one-off scan (``POST /scan``), long-poll to a terminal
state (``GET /scan/{id}?wait=N``), and read the arrays (``GET /scan/{id}/data``). The same verbs map
to a real TAS later, so the write path transfers by swapping this client.
"""
from __future__ import annotations

from isar.drive.instrument.tavi_client import TaviClient, TaviError

__all__ = ["TaviClient", "TaviError"]
