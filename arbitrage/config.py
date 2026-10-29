"""Central configuration. Values come from environment variables / the .env file
written by `python -m arbitrage setup`, with defaults that match the free-tier
accounts this project runs on (AWS Free plan = Sydney only, Azure for Students =
five allowed regions)."""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _find_cli(name: str, fallbacks: list[str]) -> str:
    """Locate a CLI even when the terminal's PATH is stale (common right after install on Windows)."""
    found = shutil.which(name)
    if found:
        return found
    for path in fallbacks:
        if Path(path).exists():
            return path
    return name


@dataclass(frozen=True)
class Candidate:
    """One place the workload could run. Every candidate meets the workload's
    minimum requirement (>= 2 vCPU, >= 2 GB RAM), so the cheapest one wins."""
    cloud: str      # "azure" | "aws"
    region: str     # Azure region or AWS region
    zone: str       # AWS availability zone ("" for Azure)
    sku: str        # VM size / instance type

    @property
    def key(self) -> str:
        return f"{self.cloud}:{self.zone or self.region}:{self.sku}"


AZURE_REGIONS = ["koreacentral", "eastasia", "uaenorth", "polandcentral", "indonesiacentral"]
AWS_REGION = "ap-southeast-2"
AWS_ZONES = ["ap-southeast-2a", "ap-southeast-2b", "ap-southeast-2c"]


@dataclass
class Settings:
    # When true, no real VMs are created: providers are simulated locally.
    dry_run: bool = _env("DRY_RUN", "true").lower() == "true"

    azure_sku: str = _env("AZURE_SKU", "Standard_F2s_v2")
    aws_sku: str = _env("AWS_SKU", "t3.small")
    azure_regions: list[str] = field(default_factory=lambda: _env("AZURE_REGIONS", ",".join(AZURE_REGIONS)).split(","))
    aws_region: str = _env("AWS_REGION", AWS_REGION)
    aws_zones: list[str] = field(default_factory=lambda: _env("AWS_ZONES", ",".join(AWS_ZONES)).split(","))

    azure_resource_group: str = _env("AZURE_RESOURCE_GROUP", "rg-cloud-arbitrage")
    azure_home_region: str = _env("AZURE_HOME_REGION", "koreacentral")
    aws_security_group: str = _env("AWS_SECURITY_GROUP", "cloud-arbitrage-sg")

    workload_image: str = _env("WORKLOAD_IMAGE", "ghcr.io/bhavishyashadani/cloud-spot-arbitrage:latest")
    workload_port: int = int(_env("WORKLOAD_PORT", "8080"))
    cosmos_endpoint: str = _env("COSMOS_ENDPOINT")
    cosmos_key: str = _env("COSMOS_KEY")

    # Cost model knobs
    min_savings_pct: float = float(_env("MIN_SAVINGS_PCT", "10"))      # ignore tiny price differences
    payback_horizon_h: float = float(_env("PAYBACK_HORIZON_H", "2"))   # migration must pay for itself within this
    boot_minutes: float = float(_env("BOOT_MINUTES", "4"))             # time both VMs run during cutover
    min_dwell_minutes: float = float(_env("MIN_DWELL_MINUTES", "10"))  # anti-flapping
    poll_seconds: int = int(_env("POLL_SECONDS", "60"))

    db_path: Path = ROOT / "arbitrage.db"
    az_cli: str = _find_cli("az", [r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"])

    def candidates(self) -> list[Candidate]:
        azure = [Candidate("azure", r, "", self.azure_sku) for r in self.azure_regions]
        aws = [Candidate("aws", self.aws_region, z, self.aws_sku) for z in self.aws_zones]
        return azure + aws


settings = Settings()
