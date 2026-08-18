"""`isar scene-build --workspace DIR` — build ONE scene revision and say what happened.

The lean build invocation the campaign parent supervises (SCENE-4.3 D1/C5, owner ruling
2026-08-18). It is deliberately NOT a second build semantics: it calls the same
``isar.session.scene_hook.build_and_store`` that `isar run` composes and that `isar report --scene`
already calls — one canonical operation, three frontends. What this command adds is a *machine*
surface: exactly one structured JSON line on stdout, so the campaign parent (which may not import
:mod:`isar.session` at all — ``tests/drive/test_boundary.py``) learns the digest it must pin
without parsing prose.

The contract, and what each field is for:

``digest``
    The revision this build COMPUTED. It is what the parent pins into ``plan_next_action``.
``pointer_flipped``
    Whether ``report/scene/current.json`` now names it because of THIS build.
``unchanged``
    Whether the pointer already named it (an idempotent rebuild of an unchanged workspace).

Exit ``0`` means "there is a durable revision at ``digest``". That INCLUDES the concurrent-write
case: ``RevisionStore.write_revision`` writes the content-addressed blob before it takes the
pointer lock, so a racing ``isar report --scene`` publish that wins the CAS leaves this build's
blob durable and addressable — only the pointer flip was lost, and the pointer is not campaign
authority (D1). Reporting that as a failure would turn a read-only CLI into a campaign-stopping
"machine failure", which the four-reasons stop law forbids. Exit ``3`` means there is NO revision:
the adapter refused the workspace, the store is corrupt, or the build raised. The parent
classifies that as a controller fault and never falls back to an older scene.
"""
from __future__ import annotations

import json
import sys

#: The one line this command prints on success. Versioned because the campaign parent parses it.
REPORT_SCHEMA = "isar.scene-build-report/1"


def run(args) -> int:
    from isar.scene.store import ConcurrentSceneWrite
    from isar.session import scene_hook

    try:
        result = scene_hook.build_and_store(args.workspace, source=str(args.source))
    except ConcurrentSceneWrite as exc:
        # Non-fault by construction (see the module docstring): the blob is durable, and the
        # exception carries the digest it was written under. Still say it on stderr — a lost flip
        # is worth seeing in a log even though it costs the campaign nothing.
        print(f"note: scene-build: {exc}", file=sys.stderr)
        if not exc.digest:
            # Defensive: an older store binding that raised without the attribute leaves us with
            # no address to pin, which IS a fault — refusing beats inventing one.
            print("error: scene-build: concurrent write reported no digest to pin",
                  file=sys.stderr)
            return 3
        _emit(exc.digest, pointer_flipped=False, unchanged=False)
        return 0
    except Exception as exc:  # noqa: BLE001 — reported to the caller, never swallowed
        print(f"error: scene-build: {exc}", file=sys.stderr)
        return 3
    _emit(result.digest, pointer_flipped=not result.unchanged, unchanged=result.unchanged)
    return 0


def _emit(digest: str, *, pointer_flipped: bool, unchanged: bool) -> None:
    print(json.dumps({"schema": REPORT_SCHEMA, "digest": str(digest),
                      "pointer_flipped": bool(pointer_flipped), "unchanged": bool(unchanged)},
                     sort_keys=True))
