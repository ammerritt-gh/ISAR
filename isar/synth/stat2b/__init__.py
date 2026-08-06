"""Evidence-only synthetic campaign support for STAT-2b."""

from .campaign import run_campaign, summarize_campaign
from .spec import load_campaign_spec

__all__ = [
    "load_campaign_spec",
    "run_campaign",
    "summarize_campaign",
]
