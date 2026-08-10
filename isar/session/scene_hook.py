"""Build one scene revision from a delivered workspace and store it (SCENE-3 PR 3c).

The single invocation path shared by the driver's observe-only hook and ``isar report --scene``,
so the two can never build the same workspace's scene under different inputs — a scene built by
the campaign and a scene built offline from the same ledger must be the same revision, and that
is only true if one function stamps both.

Every import is LOCAL to the function body on purpose. ``isar.workspace_api`` reads
``isar.session`` (the G16 allow-list), so a module-level import here would close an
``isar.session`` → ``isar.workspace_api`` → ``isar.session`` cycle at driver import time. Kept
local, an ImportError is just another failure the caller already handles.

Observe-only: this writes ``report/scene/`` and nothing else. It re-fits nothing, re-collates
nothing, and steers nothing (SCENE-3 is a passive slice).
"""
from __future__ import annotations

from pathlib import Path

from isar.session.workspace import REPORT_DIRNAME


def build_and_store(root):
    """Score the workspace at ``root`` into a scene revision and publish it.

    Returns the store's :class:`isar.scene.store.StoreResult`. Raises whatever the adapter, the
    scorer or the store raise — a workspace the adapter cannot read faithfully refuses with a
    re-deliver message (H1), and a corrupt scene pointer is a ``ValueError``. Callers decide
    whether that is fatal: the driver hook logs and skips, ``isar report --scene`` exits nonzero
    because the operator asked for a scene by name.
    """
    from isar.analyze.catalogue import DEFAULT_PROFILE
    from isar.scene import store
    from isar.scene.build import build_scene
    from isar.workspace_api import Workspace
    from isar.workspace_api.scene import sample_knowledge, scene_observations

    ws = Workspace.open(root)
    # The catalogue profile the fitter enumerates under (isar/analyze/loop.py). The scene never
    # imports the catalogue itself, so the stamp is supplied here, at the one place that knows
    # both layers.
    revision = build_scene(tuple(scene_observations(ws)),
                           catalogue_version=DEFAULT_PROFILE,
                           sample=sample_knowledge(ws))
    return store.write_revision(Path(root) / REPORT_DIRNAME, revision)
