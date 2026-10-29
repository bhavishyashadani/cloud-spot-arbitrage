"""Price Telemetry Monitor: pulls live spot prices from each cloud and
normalises them into one stream of {candidate, $/hour} records."""
import logging
import time
from datetime import datetime, timezone

import boto3
import httpx

from . import db
from .config import Candidate, settings

log = logging.getLogger("telemetry")

AZURE_PRICES_URL = "https://prices.azure.com/api/retail/prices"


def azure_spot_prices(regions: list[str], sku: str) -> dict[str, float]:
    """Azure Retail Prices API (public, no auth). Returns {region: $/hour} for Linux Spot."""
    region_filter = " or ".join(f"armRegionName eq '{r}'" for r in regions)
    odata = (f"serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
             f"and armSkuName eq '{sku}' and ({region_filter})")
    prices: dict[str, float] = {}
    url, params = AZURE_PRICES_URL, {"$filter": odata}
    while url:
        resp = httpx.get(url, params=params, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        for item in body["Items"]:
            if item["skuName"].endswith(" Spot") and "Windows" not in item["productName"]:
                prices[item["armRegionName"]] = item["retailPrice"]
        url, params = body.get("NextPageLink"), None
    return prices


def aws_spot_prices(region: str, zones: list[str], sku: str) -> dict[str, float]:
    """EC2 DescribeSpotPriceHistory, latest price per availability zone."""
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_spot_price_history(
        InstanceTypes=[sku], ProductDescriptions=["Linux/UNIX"],
        StartTime=datetime.now(timezone.utc),
    )
    prices: dict[str, float] = {}
    for p in sorted(resp["SpotPriceHistory"], key=lambda p: p["Timestamp"]):
        if p["AvailabilityZone"] in zones:
            prices[p["AvailabilityZone"]] = float(p["SpotPrice"])
    return prices


def active_shocks() -> dict[str, dict]:
    """Simulated price spikes injected from the dashboard (for demos)."""
    shocks = {k: v for k, v in db.get("shocks", {}).items() if v["until"] > time.time()}
    db.put("shocks", shocks)
    return shocks


def add_shock(key: str, multiplier: float, minutes: float) -> None:
    shocks = active_shocks()
    shocks[key] = {"multiplier": multiplier, "until": time.time() + minutes * 60}
    db.put("shocks", shocks)


def last_known(key: str) -> float | None:
    r = db.rows("SELECT raw_price FROM prices WHERE key = ? ORDER BY ts DESC LIMIT 1", key)
    return r[0]["raw_price"] if r else None


def collect() -> list[dict]:
    """Fetch, normalise, apply shocks, persist. Falls back to the last known
    price for a cloud whose API is temporarily unreachable."""
    raw: dict[str, float] = {}
    try:
        for region, price in azure_spot_prices(settings.azure_regions, settings.azure_sku).items():
            raw[Candidate("azure", region, "", settings.azure_sku).key] = price
    except Exception as e:  # noqa: BLE001 - telemetry must keep running
        log.warning("Azure price fetch failed: %s", e)
    try:
        for zone, price in aws_spot_prices(settings.aws_region, settings.aws_zones, settings.aws_sku).items():
            raw[Candidate("aws", settings.aws_region, zone, settings.aws_sku).key] = price
    except Exception as e:  # noqa: BLE001
        log.warning("AWS price fetch failed: %s", e)

    shocks = active_shocks()
    ts = time.time()
    out = []
    for cand in settings.candidates():
        raw_price = raw.get(cand.key, last_known(cand.key))
        if raw_price is None:
            continue
        price = raw_price * shocks.get(cand.key, {}).get("multiplier", 1.0)
        db.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?)", ts, cand.key, cand.cloud,
                   cand.region, cand.zone, cand.sku, price, raw_price)
        out.append({"candidate": cand, "price": price, "raw_price": raw_price,
                    "shocked": cand.key in shocks})
    return out
