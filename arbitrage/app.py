"""Arbitrage control plane: background control loop + dashboard API + traffic switcher.

Run with:  python -m arbitrage run
"""
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import cost_model, db, migration, providers, telemetry
from .config import AWS_ZONES, AZURE_ALLOWED_REGIONS, VM_CATALOG, settings
from .switcher import router as switcher_router

log = logging.getLogger("control")
_tick_lock = threading.Lock()

# Cost-model settings that can be changed from the dashboard while running.
EDITABLE = {"min_savings_pct": float, "payback_horizon_h": float, "boot_minutes": float,
            "min_dwell_minutes": float, "poll_seconds": int}


VM_EDITABLE = ("azure_sku", "aws_sku", "azure_regions", "aws_zones")


def apply_overrides() -> None:
    for name, value in db.get("settings_overrides", {}).items():
        if name in EDITABLE:
            setattr(settings, name, EDITABLE[name](value))
        elif name in VM_EDITABLE:
            setattr(settings, name, value)


def validate_vm(body: dict) -> None:
    if "azure_sku" in body and body["azure_sku"] not in VM_CATALOG["azure"]:
        raise HTTPException(400, f"unknown Azure size {body['azure_sku']}")
    if "aws_sku" in body and body["aws_sku"] not in VM_CATALOG["aws"]:
        raise HTTPException(400, f"unknown AWS type {body['aws_sku']}")
    if "azure_regions" in body and (not body["azure_regions"] or set(body["azure_regions"]) - set(AZURE_ALLOWED_REGIONS)):
        raise HTTPException(400, f"pick at least one Azure region from {', '.join(AZURE_ALLOWED_REGIONS)}")
    if "aws_zones" in body and (not body["aws_zones"] or set(body["aws_zones"]) - set(AWS_ZONES)):
        raise HTTPException(400, f"pick at least one AWS zone from {', '.join(AWS_ZONES)}")


def tick() -> None:
    """One control-loop iteration: telemetry -> eviction check -> cost model -> (maybe) migrate."""
    with _tick_lock:
        quotes = telemetry.collect()
        if migration.in_progress():
            return
        evicted = migration.check_eviction()
        active = migration.active_backend()
        decision = cost_model.evaluate(
            active and active["key"],
            [cost_model.Quote(q["candidate"].key, q["price"]) for q in quotes],
            now=time.time(), last_migration_ts=db.get("last_migration_ts", 0),
            min_savings_pct=settings.min_savings_pct, payback_horizon_h=settings.payback_horizon_h,
            boot_minutes=settings.boot_minutes, min_dwell_minutes=settings.min_dwell_minutes,
            excluded=migration.excluded_keys(),
        )
        reason = ("SPOT EVICTION detected - " if evicted else "") + decision.reason
        autopilot = db.get("autopilot", False)
        action = decision.action if autopilot or decision.action == "stay" else f"recommend-{decision.action}"
        db.execute("INSERT INTO decisions VALUES (?,?,?,?,?)", time.time(), active and active["key"],
                   decision.target, action, reason)
        if autopilot and decision.action in ("bootstrap", "migrate"):
            migration.start(decision.target, reason)


def control_loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - the loop must survive transient cloud errors
            log.exception("control loop iteration failed")
        time.sleep(settings.poll_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    apply_overrides()
    threading.Thread(target=control_loop, daemon=True).start()
    yield


app = FastAPI(title="Cloud Spot Arbitrage", lifespan=lifespan)
app.include_router(switcher_router)


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/state")
def state():
    since = time.time() - 3 * 3600
    return {
        "dry_run": settings.dry_run,
        "autopilot": db.get("autopilot", False),
        "migrating": migration.in_progress(),
        "settings": {k: getattr(settings, k) for k in EDITABLE},
        "vm": {k: getattr(settings, k) for k in VM_EDITABLE},
        "vm_options": {"catalog": VM_CATALOG, "azure_regions": AZURE_ALLOWED_REGIONS, "aws_zones": AWS_ZONES},
        "active": migration.active_backend(),
        "latest_prices": [p for p in db.rows("SELECT p.* FROM prices p JOIN (SELECT key, MAX(ts) ts FROM prices "
                                             "GROUP BY key) m ON p.key = m.key AND p.ts = m.ts ORDER BY p.price")
                          if p["key"] in {c.key for c in settings.candidates()}],
        "history": db.rows("SELECT ts, key, price FROM prices WHERE ts > ? ORDER BY ts", since),
        "shocks": telemetry.active_shocks(),
        "excluded": sorted(migration.excluded_keys()),
        "backends": db.rows("SELECT * FROM backends ORDER BY id DESC LIMIT 10"),
        "migrations": db.rows("SELECT * FROM migrations ORDER BY id DESC LIMIT 10"),
        "decisions": db.rows("SELECT * FROM decisions ORDER BY ts DESC LIMIT 15"),
    }


class Toggle(BaseModel):
    on: bool


class Shock(BaseModel):
    key: str | None = None    # one placement ...
    cloud: str | None = None  # ... or every placement of a cloud
    multiplier: float = 3.0
    minutes: float = 15


class Target(BaseModel):
    key: str


@app.post("/api/settings")
def update_settings(body: dict[str, float]):
    """Change cost-model settings at runtime; saved so they survive a restart."""
    unknown = set(body) - set(EDITABLE)
    if unknown:
        raise HTTPException(400, f"not editable: {', '.join(sorted(unknown))}")
    if any(v < 0 for v in body.values()) or body.get("poll_seconds", 10) < 10:
        raise HTTPException(400, "values must be positive and poll_seconds at least 10")
    db.put("settings_overrides", {**db.get("settings_overrides", {}), **body})
    apply_overrides()
    threading.Thread(target=tick, daemon=True).start()  # re-evaluate with the new rules now
    return {k: getattr(settings, k) for k in EDITABLE}


@app.post("/api/vm-config")
def update_vm_config(body: dict):
    """Change VM sizes / regions at runtime. If the running VM no longer matches,
    the cost model treats its placement as unavailable and migrates to a matching one."""
    unknown = set(body) - set(VM_EDITABLE)
    if unknown:
        raise HTTPException(400, f"not editable: {', '.join(sorted(unknown))}")
    validate_vm(body)
    db.put("settings_overrides", {**db.get("settings_overrides", {}), **body})
    apply_overrides()
    threading.Thread(target=tick, daemon=True).start()
    return {k: getattr(settings, k) for k in VM_EDITABLE}


@app.post("/api/autopilot")
def set_autopilot(body: Toggle):
    db.put("autopilot", body.on)
    threading.Thread(target=tick, daemon=True).start()
    return {"autopilot": body.on}


@app.post("/api/shock")
def shock(body: Shock):
    """Simulate a spot price spike on one placement or a whole cloud (demo of price-driven migration)."""
    keys = [c.key for c in settings.candidates() if c.key == body.key or c.cloud == body.cloud]
    if not keys:
        raise HTTPException(400, "give a known placement key or cloud name")
    for key in keys:
        telemetry.add_shock(key, body.multiplier, body.minutes)
    threading.Thread(target=tick, daemon=True).start()
    return {"ok": True}


@app.post("/api/evict")
def evict():
    """Simulate the cloud reclaiming the active spot VM (demo of eviction failover)."""
    active = migration.active_backend()
    if active is None:
        raise HTTPException(400, "nothing is running")
    if settings.dry_run:
        from .providers.mock import evict as mock_evict
        mock_evict(active["instance_id"])
    else:
        providers.get(active["cloud"]).terminate(active["instance_id"])
    threading.Thread(target=tick, daemon=True).start()
    return {"evicted": active["instance_id"]}


@app.post("/api/migrate")
def manual_migrate(body: Target):
    if migration.in_progress():
        raise HTTPException(409, "a migration is already running")
    migration.start(body.key, "manual migration from dashboard")
    return {"ok": True}


@app.post("/api/tick")
def run_tick():
    threading.Thread(target=tick, daemon=True).start()
    return {"ok": True}
