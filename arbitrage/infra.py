"""One-time cloud setup, status and teardown.

setup     - Azure resource group + Cosmos DB (free tier) + AWS security group, writes .env
status    - lists every billable resource this project has running
teardown  - deletes all workload VMs (add --all to also delete the datastore + resource group)
"""
import hashlib
import time

import boto3
from botocore.exceptions import ClientError

from . import db
from .config import ROOT, settings
from .providers.azure import az
from .providers.base import TAG_KEY, TAG_VALUE

COSMOS_DB, COSMOS_CONTAINER = "arbitrage", "state"


def _cosmos_name() -> str:
    sub = az("account", "show")["id"]
    return "arbitrage-" + hashlib.sha1(sub.encode()).hexdigest()[:8]


def _write_env(values: dict[str, str]) -> None:
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    existing = {l.split("=", 1)[0]: i for i, l in enumerate(lines) if "=" in l}
    for k, v in values.items():
        if k in existing:
            lines[existing[k]] = f"{k}={v}"
        else:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


def setup() -> None:
    rg, loc = settings.azure_resource_group, settings.azure_home_region
    for namespace in ("Microsoft.Compute", "Microsoft.Network", "Microsoft.DocumentDB"):
        print(f"[azure] enabling {namespace} on the subscription")
        az("provider", "register", "-n", namespace, "--wait")
    print(f"[azure] resource group {rg} ({loc})")
    az("group", "create", "-n", rg, "-l", loc, "--tags", f"{TAG_KEY}={TAG_VALUE}")

    name = _cosmos_name()
    if az("cosmosdb", "show", "-g", rg, "-n", name, check=False) is None:
        print(f"[azure] creating Cosmos DB account {name} (free tier) - this takes 5-10 minutes...")
        az("cosmosdb", "create", "-g", rg, "-n", name, "--locations", f"regionName={loc}",
           "--enable-free-tier", "true", "--default-consistency-level", "Session")
    az("cosmosdb", "sql", "database", "create", "-g", rg, "-a", name, "-n", COSMOS_DB, check=False)
    az("cosmosdb", "sql", "container", "create", "-g", rg, "-a", name, "-d", COSMOS_DB,
       "-n", COSMOS_CONTAINER, "-p", "/id", "--throughput", "400", check=False)
    endpoint = az("cosmosdb", "show", "-g", rg, "-n", name)["documentEndpoint"]
    key = az("cosmosdb", "keys", "list", "-g", rg, "-n", name)["primaryMasterKey"]
    print(f"[azure] Cosmos DB ready: {endpoint}")

    ec2 = boto3.client("ec2", region_name=settings.aws_region)
    try:
        sg = ec2.create_security_group(GroupName=settings.aws_security_group,
                                       Description="Cloud arbitrage workload (port 8080)")
        ec2.authorize_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": settings.workload_port, "ToPort": settings.workload_port,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        print(f"[aws] security group {sg['GroupId']} created")
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        print("[aws] security group already exists")

    _write_env({"COSMOS_ENDPOINT": endpoint, "COSMOS_KEY": key})
    print("Setup complete. Secrets were written to .env (git-ignored).")


def _aws_instances() -> list[dict]:
    ec2 = boto3.client("ec2", region_name=settings.aws_region)
    res = ec2.describe_instances(Filters=[
        {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    return [i for r in res["Reservations"] for i in r["Instances"]]


def _azure_vms() -> list[dict]:
    return az("vm", "list", "-g", settings.azure_resource_group, check=False) or []


def status() -> None:
    print("Azure VMs:")
    for vm in _azure_vms():
        print(f"  {vm['name']}  {vm['location']}  {vm['hardwareProfile']['vmSize']}  priority={vm.get('priority')}")
    print("AWS instances:")
    for i in _aws_instances():
        print(f"  {i['InstanceId']}  {i['Placement']['AvailabilityZone']}  {i['InstanceType']}  {i['State']['Name']}")
    if az("group", "exists", "-n", settings.azure_resource_group):
        print(f"Azure resource group {settings.azure_resource_group} exists (holds Cosmos DB - free tier)")


def teardown(everything: bool = False) -> None:
    from .providers.azure import AzureProvider
    for vm in _azure_vms():
        print(f"[azure] deleting VM {vm['name']}")
        AzureProvider().terminate(vm["name"])
    ids = [i["InstanceId"] for i in _aws_instances()]
    ec2 = boto3.client("ec2", region_name=settings.aws_region)
    if ids:
        print(f"[aws] terminating {ids}")
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
    if everything:
        try:
            ec2.delete_security_group(GroupName=settings.aws_security_group)
            print("[aws] security group deleted")
        except ClientError:
            pass
        print(f"[azure] deleting resource group {settings.azure_resource_group} (Cosmos DB included)")
        az("group", "delete", "-n", settings.azure_resource_group, "--yes", check=False)
    db.init()
    db.execute("UPDATE backends SET status = 'deleted', deleted_ts = ? WHERE status != 'deleted'", time.time())
    db.put("active_backend", None)
    db.put("autopilot", False)  # otherwise the control loop would immediately redeploy
    print("Teardown complete.")
