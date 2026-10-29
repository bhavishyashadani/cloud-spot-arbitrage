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
from .config import settings
from .switcher import router as switcher_router

log = logging.getLogger("control")
_tick_lock = threading.Lock()


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
        "settings": {k: getattr(settings, k) for k in
                     ("min_savings_pct", "payback_horizon_h", "boot_minutes", "min_dwell_minutes", "poll_seconds")},
        "active": migration.active_backend(),
        "latest_prices": db.rows("SELECT p.* FROM prices p JOIN (SELECT key, MAX(ts) ts FROM prices GROUP BY key) m "
                                 "ON p.key = m.key AND p.ts = m.ts ORDER BY p.price"),
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
    key: str
    multiplier: float = 3.0
    minutes: float = 15


class Target(BaseModel):
    key: str


@app.post("/api/autopilot")
def set_autopilot(body: Toggle):
    db.put("autopilot", body.on)
    threading.Thread(target=tick, daemon=True).start()
    return {"autopilot": body.on}


@app.post("/api/shock")
def shock(body: Shock):
    """Simulate a spot price spike on one placement (demo of price-driven migration)."""
    telemetry.add_shock(body.key, body.multiplier, body.minutes)
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
