"""Cross-Cloud State Migration Engine.

The workload is stateless on the VM: all state lives in the global datastore
(Cosmos DB), so a migration is a make-before-break cutover:
  1. provision the cheaper VM          (it boots and pulls the image from GHCR)
  2. wait until its /health responds   (image pulled, datastore reachable)
  3. update the route ("DNS record")   (switcher sends client traffic to it)
  4. drain and terminate the old VM
"""
import json
import logging
import threading
import time

import httpx

from . import db, providers
from .config import Candidate, settings

log = logging.getLogger("migration")
_running = threading.Lock()

HEALTH_TIMEOUT_S = 15 * 60
EXCLUDE_MINUTES = 30


def candidate(key: str) -> Candidate:
    return next(c for c in settings.candidates() if c.key == key)


def active_backend() -> dict | None:
    backend_id = db.get("active_backend")
    if backend_id is None:
        return None
    r = db.rows("SELECT * FROM backends WHERE id = ?", backend_id)
    return r[0] if r else None


def excluded_keys() -> frozenset[str]:
    excluded = {k: until for k, until in db.get("excluded", {}).items() if until > time.time()}
    db.put("excluded", excluded)
    return frozenset(excluded)


def exclude(key: str) -> None:
    excluded = db.get("excluded", {})
    excluded[key] = time.time() + EXCLUDE_MINUTES * 60
    db.put("excluded", excluded)


def in_progress() -> bool:
    return _running.locked()


def wait_healthy(endpoint: str) -> None:
    if endpoint.startswith("mock://"):
        return
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        try:
            if httpx.get(f"http://{endpoint}/health", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(10)
    raise TimeoutError(f"{endpoint} did not become healthy within {HEALTH_TIMEOUT_S // 60} min")


def migrate(target_key: str, reason: str) -> None:
    """Run one migration. Returns immediately if another one is in progress."""
    if not _running.acquire(blocking=False):
        return
    old = active_backend()
    steps: list[dict] = []
    mig_id = db.execute("INSERT INTO migrations(started_ts, from_key, to_key, status, reason, steps) "
                        "VALUES (?,?,?,?,?,?)", time.time(), old and old["key"], target_key,
                        "running", reason, "[]")

    def step(text: str) -> None:
        log.info("migration %s: %s", mig_id, text)
        steps.append({"ts": time.time(), "text": text})
        db.execute("UPDATE migrations SET steps = ? WHERE id = ?", json.dumps(steps), mig_id)

    new_id = None
    try:
        cand = candidate(target_key)
        provider = providers.get(cand.cloud)
        name = f"arb-{cand.cloud}-{int(time.time())}"
        step(f"1. Provisioning {cand.sku} on {cand.cloud} ({cand.zone or cand.region})")
        inst = provider.provision(cand, name)
        new_id = db.execute("INSERT INTO backends(key, cloud, region, zone, sku, instance_id, endpoint, status, "
                            "market, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)", cand.key, cand.cloud, cand.region,
                            cand.zone, cand.sku, inst.instance_id, inst.endpoint, "booting", inst.market, time.time())
        step(f"2. VM {inst.instance_id} up ({inst.market}); pulling container image and waiting for /health")
        wait_healthy(inst.endpoint)
        db.execute("UPDATE backends SET status = 'active' WHERE id = ?", new_id)
        db.put("active_backend", new_id)
        step(f"3. Route updated: client traffic now goes to {inst.endpoint}")
        if old:
            time.sleep(5)  # let in-flight requests to the old VM finish
            providers.get(old["cloud"]).terminate(old["instance_id"])
            db.execute("UPDATE backends SET status = 'deleted', deleted_ts = ? WHERE id = ?", time.time(), old["id"])
            step(f"4. Old VM {old['instance_id']} on {old['cloud']} terminated")
        db.put("last_migration_ts", time.time())
        db.execute("UPDATE migrations SET status = 'done', finished_ts = ? WHERE id = ?", time.time(), mig_id)
    except Exception as e:  # noqa: BLE001 - any failure must roll back the new VM
        log.exception("migration %s failed", mig_id)
        step(f"FAILED: {e}")
        exclude(target_key)
        if new_id is not None:
            providers.get(cand.cloud).terminate(inst.instance_id)
            db.execute("UPDATE backends SET status = 'deleted', deleted_ts = ? WHERE id = ?", time.time(), new_id)
        db.execute("UPDATE migrations SET status = 'failed', finished_ts = ? WHERE id = ?", time.time(), mig_id)
    finally:
        _running.release()


def check_eviction() -> bool:
    """True if the active VM was reclaimed by its cloud (spot eviction)."""
    backend = active_backend()
    if backend is None or backend["status"] != "active":
        return False
    if providers.get(backend["cloud"]).status(backend["instance_id"]) != "evicted":
        return False
    db.execute("UPDATE backends SET status = 'evicted', deleted_ts = ? WHERE id = ?", time.time(), backend["id"])
    db.put("active_backend", None)
    exclude(backend["key"])
    return True


def start(target_key: str, reason: str) -> None:
    threading.Thread(target=migrate, args=(target_key, reason), daemon=True).start()
