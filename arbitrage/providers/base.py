from dataclasses import dataclass

from ..config import Candidate, settings

TAG_KEY, TAG_VALUE = "project", "cloud-arbitrage"


@dataclass
class Instance:
    instance_id: str
    endpoint: str   # "host:port", or "mock://..." in dry-run mode
    market: str     # "spot" | "on-demand" ...


class Provider:
    """What the migration engine needs from a cloud."""

    def provision(self, cand: Candidate, name: str) -> Instance:
        raise NotImplementedError

    def status(self, instance_id: str) -> str:
        """'running' | 'pending' | 'evicted' (gone without us deleting it)."""
        raise NotImplementedError

    def terminate(self, instance_id: str) -> None:
        raise NotImplementedError


def startup_script(cand: Candidate, name: str) -> str:
    """Boot script for a fresh VM: install Docker, pull the workload image
    from the container registry and run it. Works on Ubuntu and Amazon Linux."""
    env = {
        "CLOUD": cand.cloud, "REGION": cand.zone or cand.region, "SKU": cand.sku, "INSTANCE": name,
        "COSMOS_ENDPOINT": settings.cosmos_endpoint, "COSMOS_KEY": settings.cosmos_key,
    }
    env_flags = " ".join(f"-e {k}='{v}'" for k, v in env.items())
    return f"""#!/bin/bash
set -eux
if command -v apt-get >/dev/null; then
  apt-get update -y && apt-get install -y docker.io
else
  dnf install -y docker
fi
systemctl enable --now docker
docker run -d --restart always -p {settings.workload_port}:8080 {env_flags} {settings.workload_image}
"""
