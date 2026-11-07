"""`cst compute setup aws-ecs`: make what an ECS compute target needs, so nobody clicks through the AWS console.

Idempotent. Each resource is looked up by name first. Captain changes only what carries its tag
(captain:purpose=cst-compute), and leaves a same-named resource it did not make alone, saying so.
Nothing here runs all the time: a Fargate task exists only while a job runs. No NAT gateway is made. In a VPC
without one, tasks get a public IP so they can reach ECR, S3 and CloudWatch (inbound traffic stays blocked).
"""
from __future__ import annotations

import hashlib
import json
import time

from . import __version__

TAG_KEY, TAG_VALUE = "captain:purpose", "cst-compute"
LOG_GROUP = "/captain-search/runner"
EXEC_ROLE = "captain-search-ecs-execution"
TASK_ROLE = "captain-search-ecs-task"
SG_NAME = "captain-search-runner"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                                                 "Action": "sts:AssumeRole"}]}


def _bucket_policy(bucket: str, prefix: str) -> dict:
    p = prefix.strip("/")
    objs = f"arn:aws:s3:::{bucket}/{p + '/' if p else ''}*"
    stmt = [{"Sid": "WorkspaceObjects", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
             "Resource": objs},
            {"Sid": "WorkspaceList", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"}]
    if p:
        stmt[1]["Condition"] = {"StringLike": {"s3:prefix": [p, f"{p}/*"]}}
    return {"Version": "2012-10-17", "Statement": stmt}


class _Steps:
    def __init__(self, dry_run: bool):
        self.dry_run, self.rows, self.notes = dry_run, [], []

    def add(self, resource: str, name: str, action: str, detail: str = ""):
        self.rows.append({"resource": resource, "name": name, "action": action, "detail": detail})

    @property
    def verb(self):
        return "would create" if self.dry_run else "created"


def _tagged_iam(iam, role: str) -> bool:
    tags = iam.list_role_tags(RoleName=role).get("Tags", [])
    return any(t["Key"] == TAG_KEY and t["Value"] == TAG_VALUE for t in tags)


def setup_aws_ecs(profile: str | None, region: str, storage_uri: str, *, cluster: str = "captain-search",
                  family: str = "captain-search-runner", repo: str = "captain-search-runner", image: str | None = None,
                  cpu: str = "1024", memory: str = "2048", subnets: list[str] | None = None,
                  security_groups: list[str] | None = None, dry_run: bool = False) -> dict:
    from . import cloud

    if not storage_uri.startswith("s3://"):
        raise ValueError("ECS jobs read the workspace from S3, so the workspace must be on s3:// "
                         f"(this one is {storage_uri}). Pass --storage s3://bucket/prefix.")
    bucket, _, prefix = storage_uri[5:].partition("/")
    sess = cloud.aws_session(profile, region)
    S = _Steps(dry_run)
    account = sess.client("sts").get_caller_identity()["Account"]
    tags_kv = [{"Key": TAG_KEY, "Value": TAG_VALUE}]

    # 1. Image registry
    ecr = sess.client("ecr")
    repo_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}"
    try:
        ecr.describe_repositories(repositoryNames=[repo])
        S.add("ECR repository", repo, "exists")
    except ecr.exceptions.RepositoryNotFoundException:
        if not dry_run:
            ecr.create_repository(repositoryName=repo, tags=tags_kv, imageScanningConfiguration={"scanOnPush": True})
        S.add("ECR repository", repo, S.verb)
    image = image or f"{repo_uri}:{__version__}"
    if image.startswith(repo_uri + ":"):
        tag = image.rsplit(":", 1)[1]
        try:
            ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
            S.add("runner image", image, "exists")
        except (ecr.exceptions.ImageNotFoundException, ecr.exceptions.RepositoryNotFoundException):
            S.add("runner image", image, "missing", "push it once: runner/build.sh --push " + image)
            S.notes.append(f"The runner image {image} is not in ECR yet. Build and push it with "
                           f"`runner/build.sh --push {image}` before the first job.")

    # 2. Logs
    logs = sess.client("logs")
    if any(g["logGroupName"] == LOG_GROUP for g in logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)["logGroups"]):
        S.add("CloudWatch log group", LOG_GROUP, "exists")
    else:
        if not dry_run:
            logs.create_log_group(logGroupName=LOG_GROUP, tags={TAG_KEY: TAG_VALUE})
            logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        S.add("CloudWatch log group", LOG_GROUP, S.verb, "kept 30 days")

    # 3. Roles
    iam = sess.client("iam")
    made_role = False

    def role(name: str, why: str) -> tuple[str, bool]:
        nonlocal made_role
        arn = f"arn:aws:iam::{account}:role/{name}"
        try:
            iam.get_role(RoleName=name)
            ours = _tagged_iam(iam, name)
            S.add("IAM role", name, "exists" if ours else "exists, not made by Captain, left alone", why)
            return arn, ours
        except iam.exceptions.NoSuchEntityException:
            if not dry_run:
                iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(TRUST), Tags=tags_kv,
                                Description=f"Captain search compute: {why}")
                made_role = True
            S.add("IAM role", name, S.verb, why)
            return arn, True

    exec_arn, exec_ours = role(EXEC_ROLE, "lets ECS pull the runner image and write its logs")
    if exec_ours and not dry_run:
        iam.attach_role_policy(RoleName=EXEC_ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
    task_arn, task_ours = role(TASK_ROLE, f"lets the job read and write {storage_uri}")
    pol_name = "workspace-" + hashlib.sha1(storage_uri.encode()).hexdigest()[:10]
    if task_ours:
        if not dry_run:
            iam.put_role_policy(RoleName=TASK_ROLE, PolicyName=pol_name, PolicyDocument=json.dumps(_bucket_policy(bucket, prefix)))
        S.add("IAM role policy", f"{TASK_ROLE}/{pol_name}", "would set" if dry_run else "set",
              f"read, write and list {storage_uri} only")
    else:
        S.notes.append(f"{TASK_ROLE} exists but Captain did not make it, so its access to {storage_uri} was not changed.")

    # 4. Cluster
    ecs = sess.client("ecs")
    cl = [c for c in ecs.describe_clusters(clusters=[cluster])["clusters"] if c["status"] == "ACTIVE"]
    if cl:
        S.add("ECS cluster", cluster, "exists")
    else:
        if not dry_run:
            ecs.create_cluster(clusterName=cluster, capacityProviders=["FARGATE", "FARGATE_SPOT"],
                               tags=[{"key": TAG_KEY, "value": TAG_VALUE}])
        S.add("ECS cluster", cluster, S.verb, "Fargate only: costs nothing while no job runs")

    # 5. Network
    ec2 = sess.client("ec2")
    public_ip = "DISABLED"
    if subnets:
        sn = ec2.describe_subnets(SubnetIds=subnets)["Subnets"]
        vpc_id = sn[0]["VpcId"]
    else:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise ValueError(f"{region} has no default VPC. Pass --subnets (and --security-group) for the VPC to use.")
        vpc_id = vpcs[0]["VpcId"]
        sn = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
        subnets = [s["SubnetId"] for s in sn]
    nat = ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "state", "Values": ["available"]}])["NatGateways"]
    if not nat:
        public_ip = "ENABLED"
        S.notes.append(f"{vpc_id} has no NAT gateway, so tasks get a public IP to reach ECR, S3 and CloudWatch. "
                       "The security group allows no inbound traffic.")
    S.add("subnets", ",".join(subnets), "use", f"in {vpc_id}, public IP {public_ip.lower()}")
    if not security_groups:
        sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]
        if sgs:
            security_groups = [sgs[0]["GroupId"]]
            S.add("security group", f"{SG_NAME} ({sgs[0]['GroupId']})", "exists")
        else:
            if not dry_run:
                g = ec2.create_security_group(GroupName=SG_NAME, VpcId=vpc_id,
                                              Description="Captain search jobs: no inbound, all outbound",
                                              TagSpecifications=[{"ResourceType": "security-group", "Tags": tags_kv}])
                security_groups = [g["GroupId"]]
            S.add("security group", SG_NAME, S.verb, "no inbound, all outbound")

    # 6. Task definition
    want = {"family": family, "taskRoleArn": task_arn, "executionRoleArn": exec_arn, "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"], "cpu": str(cpu), "memory": str(memory),
            "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            "containerDefinitions": [{"name": "runner", "image": image, "essential": True,
                                      "logConfiguration": {"logDriver": "awslogs", "options": {
                                          "awslogs-group": LOG_GROUP, "awslogs-region": region, "awslogs-stream-prefix": "cst"}}}]}
    try:
        cur = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
        cd = cur["containerDefinitions"][0]
        same = (cur.get("taskRoleArn") == task_arn and cur.get("executionRoleArn") == exec_arn and cur.get("cpu") == str(cpu)
                and cur.get("memory") == str(memory) and cd.get("image") == image and cd.get("name") == "runner"
                and (cd.get("logConfiguration") or {}).get("options", {}).get("awslogs-group") == LOG_GROUP)
    except ecs.exceptions.ClientException:
        cur, same = None, False
    if same:
        S.add("task definition", f"{family}:{cur['revision']}", "exists", f"{cpu} CPU units, {memory} MB, {image}")
    else:
        if not dry_run:
            r = ecs.register_task_definition(**want, tags=[{"key": TAG_KEY, "value": TAG_VALUE}])["taskDefinition"]
            S.add("task definition", f"{family}:{r['revision']}", "created" if cur is None else "new revision",
                  f"{cpu} CPU units, {memory} MB, {image}")
        else:
            S.add("task definition", family, "would create" if cur is None else "would add a revision",
                  f"{cpu} CPU units, {memory} MB, {image}")

    if made_role:
        time.sleep(10)  # a new IAM role takes a few seconds before ECS can assume it
    config = {"cluster": cluster, "task_definition": family, "subnets": ",".join(subnets),
              "security_groups": ",".join(security_groups or []), "assign_public_ip": public_ip, "container": "runner"}
    return {"dry_run": dry_run, "account": account, "region": region, "storage": storage_uri, "image": image,
            "steps": S.rows, "notes": S.notes, "target_config": config}
