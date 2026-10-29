from arbitrage.cost_model import Quote, evaluate

RULES = dict(min_savings_pct=10, payback_horizon_h=2, boot_minutes=4, min_dwell_minutes=10)
NOW = 100_000.0
LONG_AGO = 0.0

QUOTES = [Quote("azure:koreacentral:A2", 0.0190), Quote("aws:ap-southeast-2c:t3", 0.0150)]


def test_bootstrap_picks_cheapest():
    d = evaluate(None, QUOTES, now=NOW, last_migration_ts=LONG_AGO, **RULES)
    assert (d.action, d.target) == ("bootstrap", "aws:ap-southeast-2c:t3")


def test_migrates_when_saving_is_large_enough():
    d = evaluate("azure:koreacentral:A2", QUOTES, now=NOW, last_migration_ts=LONG_AGO, **RULES)
    assert d.action == "migrate"
    assert round(d.savings_pct, 1) == 21.1
    assert d.payback_h < 1


def test_stays_when_saving_below_threshold():
    quotes = [Quote("a", 0.0200), Quote("b", 0.0195)]
    d = evaluate("a", quotes, now=NOW, last_migration_ts=LONG_AGO, **RULES)
    assert d.action == "stay" and "threshold" in d.reason


def test_cooldown_prevents_flapping():
    d = evaluate("azure:koreacentral:A2", QUOTES, now=NOW, last_migration_ts=NOW - 60, **RULES)
    assert d.action == "stay" and "cooling down" in d.reason


def test_stays_when_already_cheapest():
    d = evaluate("aws:ap-southeast-2c:t3", QUOTES, now=NOW, last_migration_ts=LONG_AGO, **RULES)
    assert d.action == "stay" and d.target is None


def test_evicted_placement_forces_migration():
    d = evaluate("aws:ap-southeast-2c:t3", QUOTES, now=NOW, last_migration_ts=NOW,
                 excluded=frozenset({"aws:ap-southeast-2c:t3"}), **RULES)
    assert (d.action, d.target) == ("migrate", "azure:koreacentral:A2")
