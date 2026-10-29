"""DNS Failover Switcher: the single address clients use (/app/...).
It forwards each request to whichever VM the migration engine marked as
active, so a cutover is invisible to clients - like updating a DNS record,
but instant (no TTL caching)."""
import httpx
from fastapi import APIRouter, Request, Response

from . import db
from .migration import active_backend

router = APIRouter()
_client = httpx.AsyncClient(timeout=10)


def _mock_response(backend: dict) -> dict:
    visits = db.get("mock_visits", 0) + 1
    db.put("mock_visits", visits)
    return {"message": "Hello from the arbitrage workload (simulated)", "cloud": backend["cloud"],
            "region": backend["zone"] or backend["region"], "sku": backend["sku"],
            "instance": backend["instance_id"], "visits": visits, "datastore": "simulated"}


@router.api_route("/app/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def route(path: str, request: Request):
    backend = active_backend()
    if backend is None:
        return Response('{"error": "no active backend - migration in progress or nothing deployed"}',
                        status_code=503, media_type="application/json")
    if backend["endpoint"].startswith("mock://"):
        return _mock_response(backend)
    upstream = await _client.request(request.method, f"http://{backend['endpoint']}/{path}",
                                     params=request.query_params, content=await request.body())
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"),
                    headers={"X-Served-By": f"{backend['cloud']}/{backend['zone'] or backend['region']}"})
