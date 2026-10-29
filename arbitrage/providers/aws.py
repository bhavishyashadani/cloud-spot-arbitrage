"""AWS EC2 via boto3 (credentials from `aws configure`).

The AWS Free plan blocks spot launches (service control policy), so we try a
spot request first and fall back to an on-demand instance of the same type.
The cost model still prices AWS with live spot prices."""
import boto3
from botocore.exceptions import ClientError

from ..config import Candidate, settings
from .base import TAG_KEY, TAG_VALUE, Instance, Provider, startup_script

AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"


class AwsProvider(Provider):
    def __init__(self):
        self.ec2 = boto3.client("ec2", region_name=settings.aws_region)

    def _security_group_id(self) -> str:
        groups = self.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [settings.aws_security_group]}])["SecurityGroups"]
        if not groups:
            raise RuntimeError("AWS security group missing - run `python -m arbitrage setup` first")
        return groups[0]["GroupId"]

    def provision(self, cand: Candidate, name: str) -> Instance:
        ami = boto3.client("ssm", region_name=settings.aws_region).get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
        request = dict(
            ImageId=ami, InstanceType=cand.sku, MinCount=1, MaxCount=1,
            SecurityGroupIds=[self._security_group_id()],
            Placement={"AvailabilityZone": cand.zone},
            UserData=startup_script(cand, name),
            TagSpecifications=[{"ResourceType": "instance",
                                "Tags": [{"Key": TAG_KEY, "Value": TAG_VALUE}, {"Key": "Name", "Value": name}]}],
        )
        market = "spot"
        try:
            resp = self.ec2.run_instances(**request, InstanceMarketOptions={"MarketType": "spot"})
        except ClientError as e:
            if e.response["Error"]["Code"] != "UnauthorizedOperation":
                raise
            market = "on-demand (spot blocked on Free plan)"
            resp = self.ec2.run_instances(**request)
        instance_id = resp["Instances"][0]["InstanceId"]
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
        desc = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        return Instance(instance_id, f"{desc['PublicIpAddress']}:{settings.workload_port}", market)

    def status(self, instance_id: str) -> str:
        try:
            desc = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        except (ClientError, IndexError):
            return "evicted"
        state = desc["State"]["Name"]
        if state == "running":
            return "running"
        return "pending" if state == "pending" else "evicted"

    def terminate(self, instance_id: str) -> None:
        try:
            self.ec2.terminate_instances(InstanceIds=[instance_id])
        except ClientError:
            pass
