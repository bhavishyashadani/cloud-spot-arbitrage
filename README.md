# Cloud Spot Arbitrage

A control plane that watches **live spot prices on Azure and AWS** and moves a containerised workload to whichever cloud and region is cheapest at the moment. It migrates without losing state, and it fails over automatically when a spot VM is evicted.

```
Azure Retail Prices API ─┐                                ┌─> Azure Spot VMs (5 regions)
                         ├─> Price Telemetry Monitor      │
AWS Spot Price History ──┘          │                     ├─> AWS EC2 (Sydney, 3 zones)
                                    v                     │          │
                         Arbitrage Cost Model             │          │  read/write state
                                    │                     │          v
                                    v                     │   Cosmos DB (global datastore)
                    Cross-Cloud Migration Engine ─────────┤
                       │ 1. provision cheaper VM          │
                       │ 2. VM pulls image from GHCR ─────┘
                       │ 3. update route
                       v
  Client ───> DNS Failover Switcher (/app) ───> active VM
```

| Diagram component | Implementation |
|---|---|
| Azure Spot Retail API | `arbitrage/telemetry.py`: public Azure Retail Prices API, Linux Spot meters |
| AWS Spot Pricing API | `arbitrage/telemetry.py`: EC2 `DescribeSpotPriceHistory`, latest price per AZ |
| Price Telemetry Monitor | `telemetry.collect()`: normalises prices to $/hour and stores the history |
| Arbitrage Cost Model | `arbitrage/cost_model.py`: savings threshold, payback time and cooldown |
| Cross-Cloud Migration Engine | `arbitrage/migration.py`: make-before-break cutover with rollback |
| DNS Failover Switcher | `arbitrage/switcher.py`: reverse proxy that always points at the active VM |
| Multi-Cloud Container Registry | GitHub Container Registry, image built by GitHub Actions |
| Cloud workload environments | `arbitrage/providers/`: Azure (az CLI) and AWS (boto3) |
| Global Managed Datastore | Azure Cosmos DB, free tier |

## Cost model

All placements are in the same capacity class, 2 vCPU / 4 GB: Azure `Standard_A2_v2` and AWS `t3.medium`. That makes their hourly prices directly comparable. The model migrates only when **all** of these hold:

1. **Saving ≥ `MIN_SAVINGS_PCT`** (default 10%). Smaller differences are treated as noise.
2. **Payback ≤ `PAYBACK_HORIZON_H`** (default 2 h). The one-off cost of migrating is the new VM running in parallel with the old one while it boots (`BOOT_MINUTES`). The migration has to pay that back quickly.
3. **Cooldown ≥ `MIN_DWELL_MINUTES`** since the last migration. This prevents flapping.

If the active VM is evicted, the model migrates immediately and excludes the evicted placement for 30 minutes.

## Free-account constraints (and how they're handled)

- **AWS Free plan:** only `ap-southeast-2` (Sydney) is usable, and **spot launches are blocked** by an AWS service control policy. The provider tries a spot request first and falls back to on-demand. The cost model still uses real AWS spot prices.
- **Azure for Students:** VMs can only be deployed in koreacentral, eastasia, uaenorth, polandcentral and indonesiacentral. The spot quota is 3 vCPU per region.

## Running

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pytest tests           # cost model tests

.\.venv\Scripts\python -m arbitrage prices       # live prices + decision
.\.venv\Scripts\python -m arbitrage run          # dashboard on http://localhost:8000
```

`DRY_RUN=true` (the default) uses **real prices but simulated VMs**, so it costs nothing. For real VMs:

```powershell
.\.venv\Scripts\python -m arbitrage setup        # Cosmos DB + AWS security group, writes .env
# set DRY_RUN=false in .env, then:
.\.venv\Scripts\python -m arbitrage run
```

### Demo from the dashboard
1. **Autopilot ON**: deploys to the cheapest placement.
2. **Send client request**: the response shows which cloud served it and the visit counter, which is stored in Cosmos DB.
3. **Spike ×3** on the active placement: simulates a spot price surge, and the engine migrates. The visit counter continues, which shows the state survived the move.
4. **Simulate spot eviction**: deletes the active VM, and the engine fails over.

## Always clean up

```powershell
.\.venv\Scripts\python -m arbitrage status          # what is running and costing credits
.\.venv\Scripts\python -m arbitrage teardown        # delete all workload VMs
.\.venv\Scripts\python -m arbitrage teardown --all  # also delete Cosmos DB + resource group
```
