"""Azure Spot VMs, driven through the az CLI (already logged in via `az login`)."""
import json
import subprocess
import tempfile
from pathlib import Path

from ..config import Candidate, settings
from .base import TAG_KEY, TAG_VALUE, Instance, Provider, startup_script

IMAGE = "Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest"


def az(*args: str, check: bool = True) -> dict | list | None:
    proc = subprocess.run([settings.az_cli, *args, "--only-show-errors", "-o", "json"],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None


class AzureProvider(Provider):
    rg = settings.azure_resource_group

    def provision(self, cand: Candidate, name: str) -> Instance:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as f:
            f.write(startup_script(cand, name))
        try:
            vm = az("vm", "create", "-g", self.rg, "-n", name, "-l", cand.region,
                    "--image", IMAGE, "--size", cand.sku,
                    "--priority", "Spot", "--eviction-policy", "Delete", "--max-price", "-1",
                    "--admin-username", "azureuser", "--generate-ssh-keys",
                    "--custom-data", f.name, "--public-ip-sku", "Standard", "--nsg-rule", "NONE",
                    "--os-disk-delete-option", "Delete", "--nic-delete-option", "Delete",
                    "--tags", f"{TAG_KEY}={TAG_VALUE}")
        finally:
            Path(f.name).unlink(missing_ok=True)
        az("vm", "open-port", "-g", self.rg, "-n", name, "--port", str(settings.workload_port), "--priority", "900")
        return Instance(name, f"{vm['publicIpAddress']}:{settings.workload_port}", "spot")

    def status(self, instance_id: str) -> str:
        view = az("vm", "get-instance-view", "-g", self.rg, "-n", instance_id, check=False)
        if view is None:
            return "evicted"  # Spot eviction policy is Delete, so an evicted VM disappears
        codes = [s["code"] for s in view["instanceView"]["statuses"]]
        return "running" if "PowerState/running" in codes else "pending"

    def terminate(self, instance_id: str) -> None:
        az("vm", "delete", "-g", self.rg, "-n", instance_id, "--yes", check=False)
        # Networking created by `vm create` is named after the VM; remove it too.
        az("network", "public-ip", "delete", "-g", self.rg, "-n", f"{instance_id}PublicIP", check=False)
        az("network", "nsg", "delete", "-g", self.rg, "-n", f"{instance_id}NSG", check=False)
        az("network", "vnet", "delete", "-g", self.rg, "-n", f"{instance_id}VNET", check=False)
