"""The workload that gets migrated between clouds. It keeps no state on the VM:
the visit counter lives in the global datastore (Cosmos DB), so it survives
every migration - proof that state moved with the workload."""
import os

from azure.core import MatchConditions
from azure.cosmos import CosmosClient, exceptions
from fastapi import FastAPI

app = FastAPI()
INFO = {k.lower(): os.environ.get(k, "unknown") for k in ("CLOUD", "REGION", "SKU", "INSTANCE")}

container = None
if os.environ.get("COSMOS_ENDPOINT"):
    container = (CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])
                 .get_database_client("arbitrage").get_container_client("state"))
_local_visits = 0


def increment_visits() -> int:
    global _local_visits
    if container is None:
        _local_visits += 1
        return _local_visits
    while True:  # optimistic concurrency: retry if another request updated the counter first
        try:
            doc = container.read_item("counter", partition_key="counter")
        except exceptions.CosmosResourceNotFoundError:
            doc = {"id": "counter", "visits": 0, "served_by": {}}
            container.create_item(doc)
            doc = container.read_item("counter", partition_key="counter")
        doc["visits"] += 1
        doc["served_by"][INFO["cloud"]] = doc["served_by"].get(INFO["cloud"], 0) + 1
        try:
            container.replace_item("counter", doc, etag=doc["_etag"], match_condition=MatchConditions.IfNotModified)
            return doc["visits"]
        except exceptions.CosmosAccessConditionFailedError:
            continue


@app.get("/health")
def health():
    if container is not None:
        container.read()  # fails if the datastore is unreachable
    return {"status": "ok", **INFO}


@app.get("/")
def index():
    return {"message": "Hello from the arbitrage workload", **INFO, "visits": increment_visits(),
            "datastore": "cosmos-db" if container is not None else "in-memory"}
