"""Command line entry point:  python -m arbitrage <command>

  run        start the control plane + dashboard on http://localhost:8000
  prices     print current live spot prices and what the cost model would do
  setup      create the datastore and networking in Azure/AWS
  status     list every running cloud resource of this project
  teardown   delete all workload VMs   (teardown --all: delete everything)
"""
import logging
import sys


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "botocore", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        import uvicorn
        uvicorn.run("arbitrage.app:app", host="127.0.0.1", port=8000)
    elif cmd == "prices":
        import time

        from . import cost_model, db, telemetry
        from .config import settings
        db.init()
        quotes = telemetry.collect()
        for q in sorted(quotes, key=lambda q: q["price"]):
            print(f"  {q['candidate'].key:45s} ${q['price']:.5f}/h")
        current = sys.argv[2] if len(sys.argv) > 2 else None
        d = cost_model.evaluate(current, [cost_model.Quote(q["candidate"].key, q["price"]) for q in quotes],
                                now=time.time(), last_migration_ts=0, min_savings_pct=settings.min_savings_pct,
                                payback_horizon_h=settings.payback_horizon_h, boot_minutes=settings.boot_minutes,
                                min_dwell_minutes=settings.min_dwell_minutes)
        print(f"Decision: {d.action} -> {d.target}: {d.reason}")
    elif cmd in ("setup", "status", "teardown"):
        from . import infra
        if cmd == "teardown":
            infra.teardown(everything="--all" in sys.argv)
        else:
            getattr(infra, cmd)()
    else:
        print(__doc__)


main()
