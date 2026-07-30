"""Evidence-only synthetic campaign support for STAT-2b."""

from .campaign import CampaignRun, run_campaign, summarize_campaign
from .spec import CampaignSpec, ScenarioSpec, load_campaign_spec
from .truth import TruthDataset, generate_dataset

__all__ = [
    "CampaignRun",
    "CampaignSpec",
    "ScenarioSpec",
    "TruthDataset",
    "generate_dataset",
    "load_campaign_spec",
    "run_campaign",
    "summarize_campaign",
]
