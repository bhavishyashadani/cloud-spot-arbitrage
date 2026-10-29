"""Dry-run provider: pretends to create VMs so the whole control loop can be
demonstrated without spending credits. The traffic switcher answers requests
for mock:// backends itself."""
import time

from .. import db
from ..config import Candidate
from .base import Instance, Provider


class MockProvider(Provider):
    def __init__(self, cloud: str):
        self.cloud = cloud

    def provision(self, cand: Candidate, name: str) -> Instance:
        time.sleep(3)  # pretend to boot
        return Instance(name, f"mock://{cand.key}", "simulated")

    def status(self, instance_id: str) -> str:
        return "evicted" if instance_id in db.get("mock_evicted", []) else "running"

    def terminate(self, instance_id: str) -> None:
        pass


def evict(instance_id: str) -> None:
    db.put("mock_evicted", db.get("mock_evicted", []) + [instance_id])
