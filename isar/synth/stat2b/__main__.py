"""Command-line wrapper for the evidence-only STAT-2b campaign."""

from __future__ import annotations

import argparse
from pathlib import Path

from .campaign import run_campaign, summarize_campaign
from .spec import load_campaign_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="Frozen campaign TOML")
    parser.add_argument(
        "--phase", required=True, choices=("development", "heldout")
    )
    parser.add_argument("--output-root", default="out/stat2b")
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_campaign_spec(args.spec)
    run = run_campaign(
        spec,
        phase=args.phase,
        output_root=Path(args.output_root),
        workers=max(args.workers, 1),
    )
    summary = summarize_campaign(run, spec)
    print(f"STAT-2b {args.phase} complete: {run.output_dir}")
    print(
        "summary fingerprint: "
        f"{summary['development_summary_fingerprint']}"
    )
    if args.phase == "development":
        print("STOP: owner approval is required before held-out execution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
