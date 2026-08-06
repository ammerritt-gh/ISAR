"""Run the read-only EIG-4 Slice 1 campaign eligibility census."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Running ``python tools\\stats\\eig_eligibility_census.py`` otherwise places only this
# directory on sys.path, not the checkout that supplies ``isar``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from _eig_eligibility_census import (  # noqa: E402
    ConfigError,
    InvariantError,
    NoReadableCampaignError,
    OutputError,
    analyze_campaign_set,
    load_campaign_set,
    write_census_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-set", required=True, type=Path,
                        help="reviewed TOML campaign population, relative to the checkout")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="directory for the generated census bundle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        campaign_set = load_campaign_set(args.campaign_set, _REPO_ROOT)
        result = analyze_campaign_set(campaign_set, _REPO_ROOT)
        write_census_bundle(result, args.output_dir, repo_root=_REPO_ROOT)
    except ConfigError as error:
        logging.error("configuration error: %s", error)
        return 2
    except NoReadableCampaignError as error:
        logging.error("no readable campaign: %s", error)
        return 3
    except OutputError as error:
        logging.error("output error: %s", error)
        return 4
    except InvariantError as error:
        logging.error("internal invariant failed: %s", error)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
