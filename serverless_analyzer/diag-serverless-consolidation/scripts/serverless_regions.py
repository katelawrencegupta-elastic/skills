"""The regions Elastic Cloud Serverless is offered in. One home for that list.

Source of truth: https://www.elastic.co/docs/deploy-manage/deploy/elastic-cloud/regions
(the Serverless regions page). Read on 2026-11-19. Refresh `SERVERLESS_REGIONS` from the page
when it changes: the page lists "a limited number" of AWS, Azure and GCP regions and says more
will be added.

An ECH deployment in a region that is not listed is not quoted on Serverless. Its workload
stays on ECH, and the quote says so. A region the page lists but a spreadsheet estimator does not
is a different matter: that one is still priced at the provider default and flagged
(see `sizing_fill.match_region`).
"""
from __future__ import annotations

SERVERLESS_REGIONS_URL = "https://www.elastic.co/docs/deploy-manage/deploy/elastic-cloud/regions"
SERVERLESS_REGIONS_READ = "2026-11-19"

SERVERLESS_REGIONS: dict[str, frozenset[str]] = {
    "aws": frozenset({
        "ap-northeast-1", "ap-southeast-1", "ap-southeast-2", "ca-central-1", "eu-central-1",
        "eu-west-1", "eu-west-2", "us-east-1", "us-east-2", "us-west-2",
    }),
    "azure": frozenset({
        "australiaeast", "brazilsouth", "canadacentral", "centralus", "eastus", "eastus2",
        "germanywestcentral", "northeurope", "southeastasia", "spaincentral", "swedencentral",
        "uaenorth", "westus2",
    }),
    "gcp": frozenset({
        "asia-northeast1", "asia-south1", "asia-southeast1", "europe-southwest1", "europe-west1",
        "europe-west2", "europe-west3", "europe-west4", "us-central1", "us-east1", "us-east4", "us-west1",
    }),
}


def _provider(value) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("aws") or text == "amazon":
        return "aws"
    if text.startswith("azure") or text == "microsoft":
        return "azure"
    if text.startswith("gcp") or text.startswith("google"):
        return "gcp"
    return text


def serverless_supports(provider, region) -> bool | None:
    """True when the page lists the region for the provider, False when it does not.

    None when the provider or region is unknown. An unknown region is not evidence that
    Serverless lacks it, so it is never treated as unsupported.
    """
    prov = _provider(provider)
    reg = str(region or "").strip().lower()
    if prov not in SERVERLESS_REGIONS or not reg:
        return None
    return reg in SERVERLESS_REGIONS[prov]
