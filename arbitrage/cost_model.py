"""Arbitrage Cost Model: decides whether moving the workload is worth it.

A migration is only triggered when all of these hold:
  1. savings_pct  >= MIN_SAVINGS_PCT      (ignore noise-level price differences)
  2. payback_h    <= PAYBACK_HORIZON_H    (the one-off cost is recovered quickly)
  3. time since the last migration >= MIN_DWELL_MINUTES (no flapping)

The one-off migration cost is the target VM running in parallel with the old
one while it boots and passes health checks (BOOT_MINUTES). Workload state is
in the global datastore, so no data has to be copied between clouds.
"""
from dataclasses import dataclass


@dataclass
class Quote:
    key: str
    price: float  # $/hour


@dataclass
class Decision:
    action: str            # "bootstrap" | "migrate" | "stay"
    target: str | None
    reason: str
    savings_pct: float = 0.0
    hourly_savings: float = 0.0
    migration_cost: float = 0.0
    payback_h: float = 0.0


def evaluate(current_key: str | None, quotes: list[Quote], *, now: float, last_migration_ts: float,
             min_savings_pct: float, payback_horizon_h: float, boot_minutes: float,
             min_dwell_minutes: float, excluded: frozenset[str] = frozenset()) -> Decision:
    usable = sorted((q for q in quotes if q.key not in excluded), key=lambda q: q.price)
    if not usable:
        return Decision("stay", None, "no price data available")
    best = usable[0]

    if current_key is None:
        return Decision("bootstrap", best.key, f"no workload running; cheapest is {best.key} at ${best.price:.4f}/h")

    current = next((q for q in quotes if q.key == current_key), None)
    if current is None or current_key in excluded:
        return Decision("migrate", best.key, f"current placement {current_key} is unavailable")
    if best.key == current_key:
        return Decision("stay", None, "already on the cheapest placement")

    hourly_savings = current.price - best.price
    savings_pct = hourly_savings / current.price * 100
    migration_cost = best.price * boot_minutes / 60
    payback_h = migration_cost / hourly_savings
    figures = dict(savings_pct=savings_pct, hourly_savings=hourly_savings,
                   migration_cost=migration_cost, payback_h=payback_h)

    if savings_pct < min_savings_pct:
        return Decision("stay", best.key, f"saving {savings_pct:.1f}% is below the {min_savings_pct:.0f}% threshold", **figures)
    if payback_h > payback_horizon_h:
        return Decision("stay", best.key, f"payback {payback_h:.2f}h exceeds the {payback_horizon_h}h horizon", **figures)
    dwell_left = min_dwell_minutes - (now - last_migration_ts) / 60
    if dwell_left > 0:
        return Decision("stay", best.key, f"cooling down, {dwell_left:.1f} min until migrations are allowed", **figures)
    return Decision("migrate", best.key,
                    f"{best.key} is {savings_pct:.1f}% cheaper; migration pays back in {payback_h * 60:.1f} min", **figures)
