from functools import cache

from ..config import settings
from .base import Instance, Provider


@cache
def get(cloud: str) -> Provider:
    if settings.dry_run:
        from .mock import MockProvider
        return MockProvider(cloud)
    if cloud == "azure":
        from .azure import AzureProvider
        return AzureProvider()
    from .aws import AwsProvider
    return AwsProvider()


__all__ = ["Instance", "Provider", "get"]
