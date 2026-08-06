"""Audit and verify the STAT-2b.1 production source closure.

This adapter can create an unapproved candidate, but it deliberately exposes no
owner-approval command.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from _stat2b_source_closure import (  # noqa: E402
    DEFAULT_ACTIVE_PATH,
    DEFAULT_CANDIDATE_PATH,
    ClosureError,
    build_candidate,
    git_provenance,
    verify_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    candidate = commands.add_parser("candidate", help="write an unapproved closure candidate")
    candidate.add_argument("--output", default=DEFAULT_CANDIDATE_PATH)
    candidate.add_argument("--production-anchor", required=True)
    candidate.add_argument("--review-output", required=True)
    candidate.add_argument(
        "--verification-evidence",
        type=Path,
        help="strict JSON summary of completed verification; omission records pending evidence",
    )
    candidate.add_argument(
        "--change-review-file",
        type=Path,
        help="JSON mapping of changed paths to reviewed dispositions and proof tests",
    )

    audit = commands.add_parser("audit", help="compare current source with a closure manifest")
    audit.add_argument("--manifest", default=DEFAULT_ACTIVE_PATH)

    verify = commands.add_parser("verify", help="strictly authenticate a closure manifest")
    verify.add_argument(
        "--candidate",
        nargs="?",
        const=DEFAULT_CANDIDATE_PATH,
        help="verify an unapproved candidate instead of the active closure",
    )
    verify.add_argument("--manifest", default=DEFAULT_ACTIVE_PATH)
    provenance = verify.add_mutually_exclusive_group()
    provenance.add_argument(
        "--git-provenance",
        action="store_true",
        help="force Git-backed provenance even for an unapproved candidate",
    )
    provenance.add_argument(
        "--no-git-provenance",
        action="store_true",
        help="skip Git-backed provenance (candidate/in-flight manifests only)",
    )
    provenance.add_argument("--release-anchor")
    return parser


def _read_verification(path: Path | None) -> dict:
    if path is None:
        return {"status": "pending", "commands": [], "parity_checks": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureError(f"cannot read verification evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError("verification evidence must be a JSON object")
    if value.get("status") not in {"pending", "passed", "failed"}:
        raise ClosureError("verification evidence has an invalid status")
    return value


def _read_change_reviews(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureError(f"cannot read change-review mapping {path}: {exc}") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, dict)
        for key, item in value.items()
    ):
        raise ClosureError("change-review file must map paths to JSON objects")
    return value


def _print_result(result) -> None:
    label = "PASSED" if result.ok else "FAILED"
    print(f"STAT-2b production source closure: {label} ({result.status})")
    for finding in result.findings:
        print(json.dumps(finding, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "candidate":
            output = str(args.output).replace("\\", "/")
            review_output = str(args.review_output).replace("\\", "/")
            manifest, review = build_candidate(
                REPO_ROOT,
                proposed_production_anchor=args.production_anchor,
                candidate_path=output,
                review_path=review_output,
                verification_evidence=_read_verification(args.verification_evidence),
                change_reviews=_read_change_reviews(args.change_review_file),
            )
            print(f"wrote unapproved candidate: {output}")
            print(f"wrote review record: {review_output}")
            print(f"candidate fingerprint: {manifest['candidate_closure_fingerprint']}")
            print(f"review fingerprint: {review['review_record_fingerprint']}")
            return 0

        candidate_path = args.candidate if args.command == "verify" else None
        audit_candidate = (
            args.command == "audit"
            and str(args.manifest).replace("\\", "/").endswith(".candidate.json")
        )
        manifest_path = str(candidate_path or args.manifest).replace("\\", "/")
        result = verify_manifest(
            REPO_ROOT,
            manifest_path,
            candidate=candidate_path is not None or audit_candidate,
        )
        _print_result(result)
        if not result.ok:
            return 1
        # An owner-approved manifest asserts three Git-backed bindings (preceding candidate
        # commit, candidate bytes, immutable review bytes); verifying it without provenance
        # proves none of them, so provenance is the default there.
        run_provenance = (
            args.command == "verify"
            and not args.no_git_provenance
            and (args.git_provenance
                 or bool(args.release_anchor)
                 or bool(result.manifest and result.manifest.get("owner_approved"))))
        if run_provenance:
            provenance = git_provenance(
                REPO_ROOT,
                result.manifest,
                release_anchor=args.release_anchor,
            )
            _print_result(provenance)
            return 0 if provenance.ok else 1
        return 0
    except ClosureError as exc:
        logging.error("source-closure error: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
