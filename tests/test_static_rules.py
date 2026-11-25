"""Deterministic baseline rules that replace hand-written manual checks (moda-starter-1.3): generated iac_changes,
environment-aware severity, scope for account-wide records, log producers from UserData and instance roles, the new
static rules and concrete owner questions. All evidence is synthetic; no AWS calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from harness import run_python

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/aws-architecture-review/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


review = load("sr_review", "skills/aws-architecture-review/scripts/review.py")
workload_rules = sys.modules["workload_rules"]

ACCOUNT, REGION = "111111111111", "us-east-1"
ARN = f"arn:aws:{{}}:{REGION}:{ACCOUNT}:{{}}"
TOPIC = ARN.format("sns", "alerts")
LB = ARN.format("elasticloadbalancing", "loadbalancer/app/web/abc123")
TD_API = ARN.format("ecs", "task-definition/api:3")
TD_BATCH = ARN.format("ecs", "task-definition/batch:7")
TD_LOOSE = ARN.format("ecs", "task-definition/loose:1")
PROFILE = f"arn:aws:iam::{ACCOUNT}:instance-profile/chat-profile"

TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Resources:
  Repo:
    Type: AWS::ECR::Repository
    Properties:
      RepositoryName: app/api
      ImageScanningConfiguration: { ScanOnPush: false }  # scanning is off
  WorkerLogs:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /app/worker
  Db:
    Type: AWS::RDS::DBCluster
    DeletionPolicy: Delete
    Properties:
      Engine: aurora-postgresql
      BackupRetentionPeriod: 1
      DeletionProtection: false
  Main:
    Type: AWS::ECS::Cluster
    Properties:
      ClusterName: main
      ClusterSettings:
        - Name: containerInsights
          Value: disabled
  Alerts:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: alerts
      DeliveryPolicy:
        Subscription: {}
Outputs:
  TopicArn:
    Value: !Ref Alerts
"""
JSON_TEMPLATE = {"Resources": {"JsonRepo": {"Type": "AWS::ECR::Repository", "Properties": {
    "RepositoryName": "json/api", "ImageTagMutability": "MUTABLE", "ImageScanningConfiguration": {"ScanOnPush": False}}}}}
TEXT_TEMPLATE = '{"Resources": {"TextLogs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/text/logs"}}}}'
DASHBOARD = {"widgets": [
    {"type": "metric", "properties": {"title": "Service CPU", "region": REGION, "metrics": [
        ["AWS/ECS", "CPUUtilization", "ClusterName", "main", "ServiceName", "api"], ["...", "ghost"]]}},
    {"type": "metric", "properties": {"title": "ALB requests", "region": REGION, "metrics": [
        ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", "app/web/placeholder"],
        [".", "HTTPCode_Target_5XX_Count", ".", "app/web/abc123", {"stat": "Sum"}]]}},
    {"type": "metric", "properties": {"title": "Other region", "region": "eu-west-1", "metrics": [
        ["AWS/EC2", "CPUUtilization", "InstanceId", "i-elsewhere"]]}},
    {"type": "metric", "properties": {"title": "Other account", "region": REGION, "accountId": "222222222222", "metrics": [
        ["AWS/EC2", "CPUUtilization", "InstanceId", "i-other-account"]]}},
    {"type": "text", "properties": {"markdown": "notes"}}]}
CW_AGENT = '{"logs": {"logs_collected": {"files": {"collect_list": [{"file_path": "/var/log/worker.log", "log_group_name": "/app/worker"}]}}}}'


def rid(kind, native):
    return f"{ACCOUNT}:{REGION}:{kind}:{native}"


def res(kind, native, attributes, name=None, evidence=("e0001",)):
    return {"id": rid(kind, native), "type": kind, "native_id": native, "name": name or native, "account_id": ACCOUNT,
            "region": REGION, "attributes": attributes, "evidence_ids": list(evidence), "observations": {}}


def instance(native, name, state="running", az="us-east-1a", tags=(), profile=None):
    attributes = {"InstanceId": native, "InstanceType": "t3.small", "State": {"Name": state},
                  "Placement": {"AvailabilityZone": az}, "Tags": [{"Key": "Name", "Value": name}] + [{"Key": k, "Value": v} for k, v in tags]}
    if profile:
        attributes["IamInstanceProfile"] = {"Arn": profile}
    return res("ec2.instance", native, attributes)


def sg(native, name, permissions):
    return res("ec2.security-group", native, {"GroupId": native, "GroupName": name, "IpPermissions": permissions})


def tcp(port, cidrs=(), v6=(), groups=(), high=None):
    return {"IpProtocol": "tcp", "FromPort": port, "ToPort": high or port, "IpRanges": [{"CidrIp": c} for c in cidrs],
            "Ipv6Ranges": [{"CidrIpv6": c} for c in v6], "UserIdGroupPairs": [{"GroupId": g} for g in groups]}


def policy(statements):
    return {"PolicyDocument": {"Version": "2012-10-17", "Statement": statements}}


def task_definition(arn, family, revision, image, role="AppRole", log_group=None):
    container = {"name": family, "image": image}
    if log_group:
        container["logConfiguration"] = {"logDriver": "awslogs", "options": {"awslogs-group": log_group}}
    return {"taskDefinition": {"taskDefinitionArn": arn, "family": family, "revision": revision, "containerDefinitions": [container],
                               "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{role}", "runtimePlatform": {"cpuArchitecture": "ARM64"}}}


def summaries(stack, rows):
    return {"StackResourceSummaries": [{"LogicalResourceId": l, "PhysicalResourceId": p, "ResourceType": t} for l, p, t in rows]}


def workload(user_data_state="running"):
    """Resources and records that exercise every new rule; returns (resources, calls)."""
    resources = [
        instance("i-app", "worker", state=user_data_state),
        instance("i-ops", "ops-bastion", az="us-east-1b"),
        instance("i-asg", "web", tags=[("aws:autoscaling:groupName", "web-asg")]),
        instance("i-node", "node", tags=[("aws:eks:cluster-name", "k1"), ("aws:autoscaling:groupName", "eks-ng")]),
        instance("i-off", "old", state="stopped"),
        instance("i-spot", "spot-worker", tags=[("aws:ec2spot:fleet-request-id", "sfr-1")]),
        instance("i-chat", "chat", profile=PROFILE),
        res("ecs.cluster", ARN.format("ecs", "cluster/main"), {"clusterName": "main", "settings": [{"name": "containerInsights", "value": "disabled"}]}, name="main"),
        res("ecs.service", ARN.format("ecs", "service/main/api"), {"serviceName": "api", "taskDefinition": TD_API, "clusterArn": ARN.format("ecs", "cluster/main")}, name="api"),
        res("ecs.service", ARN.format("ecs", "service/main/batch"), {"serviceName": "batch", "taskDefinition": TD_BATCH, "clusterArn": ARN.format("ecs", "cluster/main")}, name="batch"),
        sg("sg-db", "db", [tcp(5432, cidrs=["10.0.1.0/24"])]),
        sg("sg-open", "open-db", [tcp(3306, cidrs=["0.0.0.0/0"])]),
        sg("sg-cache6", "cache-v6", [tcp(6379, v6=["::/0"])]),
        sg("sg-ref", "by-reference", [tcp(5432, groups=["sg-app"])]),
        sg("sg-web", "web", [tcp(443, cidrs=["10.0.0.0/16"])]),
        sg("sg-range", "wide", [tcp(0, cidrs=["192.168.0.0/16"], high=65535)]),
        res("eks.cluster", "k1", {"name": "k1", "status": "ACTIVE", "resourcesVpcConfig": {}}),
        res("eks.cluster", "k2", {"name": "k2", "status": "ACTIVE", "resourcesVpcConfig": {},
                                  "encryptionConfig": [{"resources": ["secrets"], "provider": {"keyArn": ARN.format("kms", "key/k2-key")}}]}),
        res("rds.cluster", "c1", {"Engine": "aurora-postgresql", "DBClusterIdentifier": "c1", "DBClusterMembers": [{"IsClusterWriter": True}],
                                  "BackupRetentionPeriod": 1, "DeletionProtection": False}),
        res("rds.instance", "db1", {"Engine": "aurora-postgresql", "DBInstanceClass": "db.r6g.large", "DBInstanceStatus": "available",
                                    "DBClusterIdentifier": "c1", "PerformanceInsightsEnabled": False, "MonitoringInterval": 0,
                                    "StorageEncrypted": True, "PubliclyAccessible": False, "BackupRetentionPeriod": 1}),
        res("rds.instance", "db2", {"Engine": "postgres", "DBInstanceClass": "db.t4g.micro", "DBInstanceStatus": "available", "MultiAZ": True,
                                    "PerformanceInsightsEnabled": True, "MonitoringInterval": 60, "EnabledCloudwatchLogsExports": ["postgresql"],
                                    "StorageEncrypted": True, "PubliclyAccessible": False, "BackupRetentionPeriod": 7}),
        res("rds.instance", "db3", {"Engine": "aurora-postgresql", "DBInstanceClass": "db.serverless", "DBInstanceStatus": "available",
                                    "DBClusterIdentifier": "c1", "PerformanceInsightsEnabled": True, "StorageEncrypted": True,
                                    "PubliclyAccessible": False, "BackupRetentionPeriod": 1}),
        res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1"}),
        res("ec2.vpc", "vpc-2", {"VpcId": "vpc-2"}),
        res("ec2.nat-gateway", "nat-a", {"VpcId": "vpc-1", "State": "available"}),
        res("ec2.nat-gateway", "nat-b", {"VpcId": "vpc-1", "State": "available"}),
        res("ec2.nat-gateway", "nat-c", {"VpcId": "vpc-2", "State": "available"}),
        res("logs.group", "/app/worker", {"logGroupName": "/app/worker"}),
        res("logs.group", "/app/chat", {"logGroupName": "/app/chat", "retentionInDays": 14}),
        res("logs.group", "/text/logs", {"logGroupName": "/text/logs"}),
        res("elbv2.load-balancer", LB, {"Type": "application", "Scheme": "internal", "LoadBalancerName": "web", "LoadBalancerArn": LB}, name="web"),
        res("ecr.repository", ARN.format("ecr", "repository/app/api"), {"repositoryName": "app/api"}, name="app/api"),
        res("ecr.repository", ARN.format("ecr", "repository/json/api"), {"repositoryName": "json/api"}, name="json/api"),
        res("ecr.repository", ARN.format("ecr", "repository/cdk-bootstrap"), {"repositoryName": "cdk-bootstrap"}, name="cdk-bootstrap"),
        res("cloudwatch.alarm", "cpu", {"AlarmName": "cpu-high", "AlarmActions": [TOPIC]}, name="cpu-high"),
        res("sns.topic", TOPIC, {"TopicArn": TOPIC}),
    ]
    calls = [
        {"id": "e0001", "service": "ec2", "operation": "describe-instances", "data": {}},
        {"id": "e0002", "service": "ecs", "operation": "list-clusters", "data": {}},
        {"id": "e0003", "service": "elbv2", "operation": "describe-load-balancers", "data": {}},
        {"id": "e0004", "service": "ecr", "operation": "describe-repositories", "data": {"repositories": [
            {"repositoryArn": ARN.format("ecr", "repository/app/api"), "repositoryName": "app/api", "imageTagMutability": "MUTABLE",
             "imageScanningConfiguration": {"scanOnPush": False}},
            {"repositoryArn": ARN.format("ecr", "repository/json/api"), "repositoryName": "json/api", "imageTagMutability": "MUTABLE",
             "imageScanningConfiguration": {"scanOnPush": False}},
            {"repositoryArn": ARN.format("ecr", "repository/cdk-bootstrap"), "repositoryName": "cdk-bootstrap", "imageTagMutability": "MUTABLE",
             "imageScanningConfiguration": {"scanOnPush": False}},
            {"repositoryArn": ARN.format("ecr", "repository/not-listed"), "repositoryName": "not-listed", "imageTagMutability": "IMMUTABLE",
             "imageScanningConfiguration": {"scanOnPush": True}}]}},
        {"id": "e0005", "service": "elasticache", "operation": "describe-replication-groups", "data": {"ReplicationGroups": [
            {"ReplicationGroupId": "rg-1", "ARN": ARN.format("elasticache", "replicationgroup:rg-1"), "TransitEncryptionEnabled": False,
             "AuthTokenEnabled": False, "MultiAZ": "disabled", "AutomaticFailover": "disabled", "NodeGroups": []}]}},
        {"id": "s0001", "service": "cloudformation", "operation": "get-template", "arguments": ["--stack-name", "app", "--template-stage", "Original"],
         "data": {"TemplateBody": TEMPLATE}},
        {"id": "s0002", "service": "cloudformation", "operation": "list-stack-resources", "arguments": ["--stack-name", "app"],
         "data": summaries("app", [("Repo", "app/api", "AWS::ECR::Repository"), ("WorkerLogs", "/app/worker", "AWS::Logs::LogGroup"),
                                   ("Db", "c1", "AWS::RDS::DBCluster"), ("Main", "main", "AWS::ECS::Cluster"), ("Alerts", TOPIC, "AWS::SNS::Topic")])},
        {"id": "s0003", "service": "cloudformation", "operation": "get-template", "arguments": ["--stack-name", "json-stack", "--template-stage", "Original"],
         "data": {"TemplateBody": JSON_TEMPLATE}},
        {"id": "s0004", "service": "cloudformation", "operation": "list-stack-resources", "arguments": ["--stack-name", "json-stack"],
         "data": summaries("json-stack", [("JsonRepo", "json/api", "AWS::ECR::Repository")])},
        {"id": "s0005", "service": "cloudformation", "operation": "get-template", "arguments": ["--stack-name", "text-stack", "--template-stage", "Original"],
         "data": {"TemplateBody": TEXT_TEMPLATE}},
        {"id": "s0006", "service": "cloudformation", "operation": "list-stack-resources", "arguments": ["--stack-name", "text-stack"],
         "data": summaries("text-stack", [("TextLogs", "/text/logs", "AWS::Logs::LogGroup")])},
        {"id": "s0007", "service": "ecs", "operation": "describe-task-definition", "arguments": ["--task-definition", TD_API],
         "data": task_definition(TD_API, "api", 3, f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/app/api:latest")},
        {"id": "s0008", "service": "ecs", "operation": "describe-task-definition", "arguments": ["--task-definition", TD_BATCH],
         "data": task_definition(TD_BATCH, "batch", 7, f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/app/batch@sha256:{'a' * 64}", role="BatchRole")},
        {"id": "s0009", "service": "ecs", "operation": "describe-task-definition", "arguments": ["--task-definition", TD_LOOSE],
         "data": task_definition(TD_LOOSE, "loose", 1, "nginx", role="LooseRole")},
        {"id": "s0010", "service": "eks", "operation": "list-access-entries", "arguments": ["--cluster-name", "k1"],
         "data": {"accessEntries": [f"arn:aws:iam::{ACCOUNT}:role/NodeRole", f"arn:aws:iam::{ACCOUNT}:user/deployer"]}},
        {"id": "s0011", "service": "eks", "operation": "list-access-entries", "arguments": ["--cluster-name", "k2"],
         "data": {"accessEntries": [f"arn:aws:iam::{ACCOUNT}:role/NodeRole"]}},
        {"id": "s0012", "service": "cloudwatch", "operation": "get-dashboard", "arguments": ["--dashboard-name", "overview"],
         "data": {"DashboardName": "overview", "DashboardBody": json.dumps(DASHBOARD)}},
        {"id": "s0013", "service": "cloudwatch", "operation": "get-dashboard", "arguments": ["--dashboard-name", "healthy"],
         "data": {"DashboardName": "healthy", "DashboardBody": json.dumps({"widgets": [{"type": "metric", "properties": {
             "region": REGION, "metrics": [["AWS/ApplicationELB", "RequestCount", "LoadBalancer", "app/web/abc123"]]}}]})}},
        {"id": "s0014", "service": "iam", "operation": "get-role-policy", "arguments": ["--role-name", "DeployRole", "--policy-name", "deploy"],
         "provenance": {"origin": "role/DeployRole"}, "data": policy([{"Effect": "Allow", "Action": ["ecs:UpdateService", "iam:PassRole"], "Resource": "*",
                                                                      "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}}])},
        {"id": "s0015", "service": "iam", "operation": "get-role-policy", "arguments": ["--role-name", "ScopedRole", "--policy-name", "scoped"],
         "provenance": {"origin": "role/ScopedRole"}, "data": policy([{"Effect": "Allow", "Action": "iam:PassRole", "Resource": f"arn:aws:iam::{ACCOUNT}:role/AppRole"}])},
        {"id": "s0016", "service": "iam", "operation": "get-role-policy", "arguments": ["--role-name", "AdminRole", "--policy-name", "admin"],
         "provenance": {"origin": "role/AdminRole"}, "data": policy([{"Effect": "Allow", "Action": "iam:*", "Resource": "*"}])},
        {"id": "s0017", "service": "iam", "operation": "get-role-policy", "arguments": ["--role-name", "AppRole", "--policy-name", "app"],
         "provenance": {"origin": "role/AppRole"}, "data": policy([{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}])},
        {"id": "s0018", "service": "iam", "operation": "get-role-policy", "arguments": ["--role-name", "ChatRole", "--policy-name", "chat"],
         "provenance": {"origin": "role/ChatRole"}, "data": policy([
             {"Effect": "Allow", "Action": "bedrock:*", "Resource": "*"},
             {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": ARN.format("logs", "log-group:/app/chat:*")}])},
        {"id": "s0019", "service": "iam", "operation": "get-instance-profile", "arguments": ["--instance-profile-name", "chat-profile"],
         "data": {"InstanceProfile": {"Arn": PROFILE, "InstanceProfileName": "chat-profile", "Roles": [{"RoleName": "ChatRole"}]}}},
        {"id": "s0020", "service": "ec2", "operation": "describe-instance-attribute", "arguments": ["--instance-id", "i-app", "--attribute", "userData"],
         "data": {"InstanceId": "i-app", "UserData": {"Decoded": "#!/bin/bash\n"
                                                                  "curl -fsSL https://raw.githubusercontent.com/example/tool/main/install.sh | sudo INSTALL_DIR=/opt bash\n"
                                                                  f"cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<'EOF'\n{CW_AGENT}\nEOF\n"}}},
        {"id": "s0021", "service": "ec2", "operation": "describe-instance-attribute", "arguments": ["--instance-id", "i-chat", "--attribute", "userData"],
         "data": {"InstanceId": "i-chat", "UserData": {"Decoded": "#!/bin/bash\ncurl -fsSL https://example.com/releases/v1.2.3/setup.sh | sh\n"}}},
        {"id": "s0022", "service": "ec2", "operation": "describe-instance-attribute", "arguments": ["--instance-id", "i-asg", "--attribute", "userData"],
         "data": {"InstanceId": "i-asg", "UserData": {"Decoded": "#!/bin/bash\ncurl -fsSLo /tmp/app.tgz https://example.com/app.tgz\n"}}},
        {"id": "s0023", "service": "logs", "operation": "describe-log-streams", "arguments": ["--log-group-name", "/app/worker"], "data": {"logStreams": []}},
        {"id": "s0024", "service": "logs", "operation": "describe-log-streams", "arguments": ["--log-group-name", "/app/chat"], "data": {"logStreams": []}},
        {"id": "s0025", "service": "sns", "operation": "list-subscriptions-by-topic", "arguments": ["--topic-arn", TOPIC], "data": {"Subscriptions": []}},
    ]
    return resources, calls


def write_snapshot(root, resources, calls):
    """Inventory, manifest and raw files that review.verify_inputs accepts."""
    (root / "raw").mkdir(parents=True, exist_ok=True)
    records = []
    for call in calls:
        body = json.dumps(call["data"]).encode()
        path = f"raw/{call['id']}.json"
        (root / path).write_bytes(body)
        record = {"id": call["id"], "service": call["service"], "operation": call["operation"], "region": call.get("region", REGION),
                  "arguments": call.get("arguments", []), "status": "ok", "error_code": None, "raw_path": path, "raw_format": "json",
                  "sha256": hashlib.sha256(body).hexdigest(), "resource_ids": call.get("resource_ids", []), "provenance": call.get("provenance", {"origin": "workload"})}
        records.append(record)
    common = {"schema_version": "1.0", "run_id": "sr", "account_id": ACCOUNT, "regions": [REGION],
              "services": ["ec2", "ecs", "elbv2", "rds", "eks", "ecr", "logs", "iam", "cloudwatch", "sns", "elasticache"]}
    inventory = dict(common, collection_status="complete", resources=resources, relationships=[])
    manifest = dict(common, status="complete", completed_at="2026-11-20T00:00:00Z", calls=records, truncations=[], coverage={"ok": len(records)})
    (root / "inventory.json").write_text(json.dumps(inventory))
    (root / "collection-manifest.json").write_text(json.dumps(manifest))


class StaticRules(unittest.TestCase):
    """One synthetic snapshot evaluated once per environment."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "snap"
        write_snapshot(cls.root, *workload())
        cls.inventory, cls.manifest, cls.calls = review.verify_inputs(cls.root)
        cls.scoped = {r["id"] for r in cls.inventory["resources"] if "cdk-bootstrap" not in r["id"]}
        cls.runs = {}
        for env in ("non-production", "production", "unknown"):
            checks = review.evaluate(cls.inventory, cls.manifest, cls.calls, cls.root, cls.scoped, {"workload_name": "orders", "environment": env})
            review.score(checks, cls.calls, {r["id"] for r in cls.inventory["resources"]})  # the review's own validation accepts every check
            review.validate_iac_changes(checks, cls.root, cls.calls)
            cls.runs[env] = checks

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def checks(self, rule, env="non-production"):
        return [c for c in self.runs[env] if c["rule_id"] == rule]

    def one(self, rule, key, env="non-production"):
        hits = [c for c in self.checks(rule, env) if key in (c.get("resource_id") or "") or key in (c.get("target") or "")]
        self.assertEqual(len(hits), 1, (rule, key, [c.get("resource_id") or c.get("target") for c in self.checks(rule, env)]))
        return hits[0]

    def test_single_instance_outside_an_auto_scaling_group(self):
        app = self.one("EC2_SINGLE_INSTANCE_NO_ASG", rid("ec2.instance", "i-chat"))
        self.assertEqual((app["status"], app["severity"]), ("WARN", "MEDIUM"))
        self.assertIn("outside any Auto Scaling group in us-east-1a", app["observation"])
        self.assertIn("what it serves stops until it is replaced", app["observation"])
        ops = self.one("EC2_SINGLE_INSTANCE_NO_ASG", rid("ec2.instance", "i-ops"))
        self.assertEqual((ops["status"], ops["severity"]), ("WARN", "LOW"))
        self.assertIn("administration host", ops["observation"])
        self.assertEqual(self.one("EC2_SINGLE_INSTANCE_NO_ASG", rid("ec2.instance", "i-asg"))["status"], "PASS")
        judged = {c["resource_id"] for c in self.checks("EC2_SINGLE_INSTANCE_NO_ASG")}
        self.assertNotIn(rid("ec2.instance", "i-node"), judged, "EKS nodes are judged by their node group")
        self.assertNotIn(rid("ec2.instance", "i-off"), judged, "only running instances")
        self.assertNotIn(rid("ec2.instance", "i-spot"), judged, "a fleet replaces its own instances")

    def test_image_tags_that_do_not_name_one_build(self):
        latest = self.one("IMAGE_TAG_TRACEABILITY", rid("ecs.service", ARN.format("ecs", "service/main/api")))
        self.assertEqual((latest["status"], latest["severity"]), ("WARN", "MEDIUM"))
        self.assertIn("app/api:latest", latest["observation"])
        self.assertIn("s0007", latest["evidence_ids"])
        self.assertEqual(self.one("IMAGE_TAG_TRACEABILITY", rid("ecs.service", ARN.format("ecs", "service/main/batch")))["status"], "PASS")
        untagged = self.one("IMAGE_TAG_TRACEABILITY", "ECS task definition loose:1")
        self.assertEqual(untagged["status"], "WARN")
        self.assertIn("nginx:(no tag)", untagged["observation"])
        self.assertEqual(workload_rules.image_reference("registry.example.com:5000/team/app:v1"), ("team/app", "v1", None))
        self.assertEqual(workload_rules.image_reference("app@sha256:abc"), ("app", None, "sha256:abc"))

    def test_data_ports_opened_to_cidr_ranges(self):
        private = self.one("SG_CIDR_INSTEAD_OF_REFERENCE", rid("ec2.security-group", "sg-db"))
        self.assertEqual((private["status"], private["severity"]), ("WARN", "MEDIUM"))
        for text in ("db", "sg-db", "tcp 5432", "10.0.1.0/24"):
            self.assertIn(text, private["observation"])
        for group in ("sg-open", "sg-cache6"):
            public = self.one("SG_CIDR_INSTEAD_OF_REFERENCE", rid("ec2.security-group", group))
            self.assertEqual((public["status"], public["severity"]), ("FAIL", "HIGH"), group)
        self.assertIn("::/0", self.one("SG_CIDR_INSTEAD_OF_REFERENCE", rid("ec2.security-group", "sg-cache6"))["observation"])
        self.assertIn("includes", self.one("SG_CIDR_INSTEAD_OF_REFERENCE", rid("ec2.security-group", "sg-range"))["observation"])
        judged = {c["resource_id"] for c in self.checks("SG_CIDR_INSTEAD_OF_REFERENCE")}
        self.assertNotIn(rid("ec2.security-group", "sg-ref"), judged, "a security-group reference is the recommended form")
        self.assertNotIn(rid("ec2.security-group", "sg-web"), judged, "443 is not a data port")

    def test_eks_iam_users_and_secrets_encryption(self):
        user = self.one("EKS_IAM_USER_ACCESS", rid("eks.cluster", "k1"))
        self.assertEqual((user["status"], user["severity"]), ("WARN", "LOW"))
        self.assertIn(f"arn:aws:iam::{ACCOUNT}:user/deployer", user["observation"])
        self.assertNotIn("deployer", json.dumps({k: v for k, v in user.items() if k != "observation"}), "the user appears only in the ARN text")
        self.assertEqual(self.one("EKS_IAM_USER_ACCESS", rid("eks.cluster", "k2"))["status"], "PASS")
        plain = self.one("EKS_SECRETS_ENCRYPTION", rid("eks.cluster", "k1"))
        self.assertEqual((plain["status"], plain["severity"]), ("WARN", "LOW"))
        self.assertIn("EKS.3", plain["observation"])
        self.assertEqual(self.one("EKS_SECRETS_ENCRYPTION", rid("eks.cluster", "k2"))["status"], "PASS")

    def test_database_diagnostics(self):
        off = self.one("DB_DIAGNOSTICS", rid("rds.instance", "db1"))
        self.assertEqual((off["status"], off["severity"]), ("WARN", "LOW"))
        for text in ("PerformanceInsightsEnabled=False", "MonitoringInterval=0", "EnabledCloudwatchLogsExports=none"):
            self.assertIn(text, off["observation"])
        self.assertIn("Performance Insights", off["recommendation"])
        self.assertEqual(self.one("DB_DIAGNOSTICS", rid("rds.instance", "db2"))["status"], "PASS")

    def test_dashboard_widgets_that_name_missing_resources(self):
        bad = self.one("DASHBOARD_UNKNOWN_RESOURCE", "CloudWatch dashboard overview")
        self.assertEqual((bad["status"], bad["severity"]), ("WARN", "LOW"))
        self.assertIn("LoadBalancer=app/web/placeholder", bad["observation"])
        self.assertIn("ServiceName=ghost", bad["observation"], "the ... shorthand row is expanded")
        self.assertNotIn("abc123", bad["observation"], "the . shorthand row names the existing load balancer")
        self.assertNotIn("i-elsewhere", bad["observation"], "regions outside the inventory are not judged")
        self.assertNotIn("i-other-account", bad["observation"], "cross-account widgets are not judged")
        self.assertEqual(self.one("DASHBOARD_UNKNOWN_RESOURCE", "CloudWatch dashboard healthy")["status"], "PASS")
        rows = workload_rules.expand_metrics([["NS", "M", "A", "1", "B", "2"], ["...", "3"], [".", "N", ".", "4", {"stat": "Sum"}], [{"expression": "m1"}]])
        self.assertEqual(rows, [["NS", "M", "A", "1", "B", "2"], ["NS", "M", "A", "1", "B", "3"], ["NS", "N", "A", "4"]])

    def test_pass_role_on_any_role(self):
        deploy = self.one("IAM_PASSROLE_ANY", "role/DeployRole")
        self.assertEqual((deploy["status"], deploy["severity"]), ("WARN", "LOW"), "an iam:PassedToService condition narrows it; without one it is MEDIUM")
        self.assertIn("ecs-tasks.amazonaws.com", deploy["observation"])
        targets = {c["target"] for c in self.checks("IAM_PASSROLE_ANY")}
        self.assertFalse([t for t in targets if "ScopedRole" in t], "a listed role ARN is fine")
        self.assertFalse([t for t in targets if "AdminRole" in t], "iam:* on * is IAM_WILDCARD_PERMISSION's finding")
        self.assertEqual(self.one("IAM_WILDCARD_PERMISSION", "role/AdminRole")["status"], "FAIL")
        self.assertFalse([t for t in {c["target"] for c in self.checks("IAM_WILDCARD_PERMISSION")} if "DeployRole" in t],
                         "iam:PassRole alone is not a service-wide wildcard")

    def test_iam_wildcard_names_the_resources_that_use_the_role(self):
        app = self.one("IAM_WILDCARD_PERMISSION", "role/AppRole")
        self.assertIn("Used by ECS service api (task role)", app["observation"])
        self.assertIn("s0007", app["evidence_ids"])
        chat = self.one("IAM_WILDCARD_PERMISSION", "role/ChatRole")
        self.assertIn("EC2 instance i-chat (chat) through instance profile chat-profile", chat["observation"])
        self.assertIn("s0019", chat["evidence_ids"])

    def test_userdata_that_pipes_a_download_into_a_shell(self):
        moving = self.one("BOOTSTRAP_UNPINNED_SCRIPT", rid("ec2.instance", "i-app"))
        self.assertEqual((moving["status"], moving["severity"]), ("WARN", "MEDIUM"))
        self.assertIn("https://raw.githubusercontent.com/example/tool/main/install.sh into bash", moving["observation"])
        self.assertIn("from a moving branch", moving["observation"])
        pinned = self.one("BOOTSTRAP_UNPINNED_SCRIPT", rid("ec2.instance", "i-chat"))
        self.assertEqual(pinned["status"], "WARN")
        self.assertIn("https://example.com/releases/v1.2.3/setup.sh into sh", pinned["observation"])
        self.assertNotIn("moving branch", pinned["observation"])
        self.assertEqual([c for c in self.checks("BOOTSTRAP_UNPINNED_SCRIPT", "non-production") if rid("ec2.instance", "i-asg") in (c.get("resource_id") or "")], [],
                         "a download into a file is not piped, and the absence of a pattern is not reported as a pass")
        self.assertTrue(workload_rules.moving_ref("https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3"))
        self.assertTrue(workload_rules.moving_ref("https://example.com/tool/latest/install.sh"))
        self.assertFalse(workload_rules.moving_ref("https://raw.githubusercontent.com/helm/helm/v3.15.2/scripts/get-helm-3"))
        self.assertFalse(workload_rules.moving_ref("https://raw.githubusercontent.com/o/r/" + "a" * 40 + "/install.sh"))

    def test_non_production_cost_options(self):
        nat = self.one("NAT_PER_AZ_NONPROD", rid("ec2.vpc", "vpc-1"))
        self.assertEqual((nat["status"], nat["severity"], nat["pillar"]), ("WARN", "LOW", "Cost Optimization"))
        self.assertIn("depend on one AZ", nat["recommendation"])
        self.assertEqual(self.one("NAT_PER_AZ_NONPROD", rid("ec2.vpc", "vpc-2"))["status"], "PASS")
        aurora = self.one("AURORA_NONPROD_CAPACITY", rid("rds.instance", "db1"))
        self.assertEqual((aurora["status"], aurora["severity"]), ("WARN", "LOW"))
        self.assertIn("Serverless v2", aurora["recommendation"])
        self.assertNotIn(rid("rds.instance", "db3"), {c["resource_id"] for c in self.checks("AURORA_NONPROD_CAPACITY")}, "already serverless")
        self.assertNotIn(rid("rds.instance", "db2"), {c["resource_id"] for c in self.checks("AURORA_NONPROD_CAPACITY")}, "not Aurora")
        for env in ("production", "unknown"):
            self.assertFalse(self.checks("NAT_PER_AZ_NONPROD", env) + self.checks("AURORA_NONPROD_CAPACITY", env), env)

    def test_severity_follows_the_stated_environment_except_security(self):
        single = self.one("AURORA_SINGLE_INSTANCE", rid("rds.cluster", "c1"))
        self.assertEqual(single["severity"], "MEDIUM")
        self.assertEqual(single["severity_basis"], "HIGH in production; MEDIUM because the scope states a non-production environment")
        alarm = self.one("ALARM_NOTIFICATION_TARGET", TOPIC)
        self.assertEqual((alarm["pillar"], alarm["severity"]), ("Operational Excellence", "MEDIUM"))
        tls = self.one("ELASTICACHE_TRANSIT_ENCRYPTION", "rg-1")
        self.assertEqual((tls["pillar"], tls["severity"]), ("Security", "HIGH"))
        self.assertNotIn("severity_basis", tls)
        self.assertEqual(self.one("SG_CIDR_INSTEAD_OF_REFERENCE", rid("ec2.security-group", "sg-open"))["severity"], "HIGH")
        for env in ("production", "unknown"):
            other = self.one("AURORA_SINGLE_INSTANCE", rid("rds.cluster", "c1"), env)
            self.assertEqual(other["severity"], "HIGH", env)
            self.assertNotIn("severity_basis", other)
        self.assertFalse([c for c in self.runs["non-production"] if c["severity"] == "HIGH" and c["pillar"] != "Security"])

    def test_account_wide_records_skip_repositories_outside_the_scope(self):
        targets = {c["target"] for c in self.checks("ECR_SCAN_ON_PUSH")}
        self.assertNotIn(ARN.format("ecr", "repository/cdk-bootstrap"), targets, "an inventory resource outside the scope")
        self.assertIn(ARN.format("ecr", "repository/app/api"), targets)
        self.assertIn(ARN.format("ecr", "repository/not-listed"), targets, "an item that is no inventory resource keeps today's behaviour")
        everything = review.evaluate(self.inventory, self.manifest, self.calls, self.root, None, None)
        self.assertIn(ARN.format("ecr", "repository/cdk-bootstrap"), {c.get("target") for c in everything if c["rule_id"] == "ECR_SCAN_ON_PUSH"},
                      "without a scope everything is in scope")

    def test_log_producer_from_userdata_and_instance_role(self):
        worker = self.one("LOG_DELIVERY", rid("logs.group", "/app/worker"))
        self.assertEqual(worker["status"], "WARN")
        self.assertIn("EC2 instance i-app (its UserData configures this log group) is running", worker["observation"])
        self.assertIn("s0020", worker["evidence_ids"])
        self.assertNotIn("question", worker)
        chat = self.one("LOG_DELIVERY", rid("logs.group", "/app/chat"))
        self.assertEqual(chat["status"], "WARN")
        self.assertIn("its instance role may write to this log group", chat["observation"])
        self.assertTrue({"s0018", "s0019"} <= set(chat["evidence_ids"]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "snap"
            write_snapshot(root, *workload(user_data_state="stopped"))
            inventory, manifest, calls = review.verify_inputs(root)
            checks = review.evaluate(inventory, manifest, calls, root, None, None)
            stopped = next(c for c in checks if c["rule_id"] == "LOG_DELIVERY" and c["resource_id"] == rid("logs.group", "/app/worker"))
            self.assertEqual(stopped["status"], "UNKNOWN", "a stopped instance is not a producer")
            self.assertTrue(stopped["question"].endswith("?"))

    def test_owner_questions_name_the_workload_resources(self):
        by_rule = {c["rule_id"]: c for c in self.runs["non-production"] if c["rule_id"] in review.owner_questions([], None)}
        self.assertEqual(set(by_rule), {"OPS_PROCEDURES", "REL_RESTORE_TEST", "PERF_MEASUREMENT", "COST_WORKLOAD", "SUS_UTILIZATION"})
        for c in by_rule.values():
            self.assertEqual((c["status"], c["resource_id"]), ("UNKNOWN", None))
            self.assertNotIn("target", c)
            self.assertTrue(c["question"].endswith("?"), c["question"])
            self.assertEqual(c["question"], c["recommendation"])
        self.assertIn("ECS service api", by_rule["OPS_PROCEDURES"]["question"])
        self.assertIn("cpu-high", by_rule["OPS_PROCEDURES"]["question"])
        self.assertNotIn("ops-bastion", by_rule["OPS_PROCEDURES"]["question"], "an administration host has no deployment runbook")
        self.assertIn("Aurora cluster c1", by_rule["REL_RESTORE_TEST"]["question"])
        self.assertIn("non-production orders environment", by_rule["REL_RESTORE_TEST"]["question"])
        self.assertIn("outside working hours", by_rule["SUS_UTILIZATION"]["question"])
        self.assertIn("budget", by_rule["COST_WORKLOAD"]["question"])
        production = {c["rule_id"]: c for c in self.runs["production"] if c["rule_id"] in by_rule}
        self.assertIn("How busy are", production["SUS_UTILIZATION"]["question"])
        self.assertNotIn("non-production", " ".join(c["question"] for c in production.values()))


class GeneratedTemplateChanges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "snap"
        write_snapshot(cls.root, *workload())
        cls.inventory, cls.manifest, cls.calls = review.verify_inputs(cls.root)
        cls.checks = review.evaluate(cls.inventory, cls.manifest, cls.calls, cls.root, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def change(self, rule, key):
        hits = [c for c in self.checks if c["rule_id"] == rule and (key in (c.get("resource_id") or "") or key in (c.get("target") or ""))]
        self.assertEqual(len(hits), 1, (rule, key))
        return hits[0]

    def test_yaml_template_values_are_read_literally(self):
        scan = self.change("ECR_SCAN_ON_PUSH", ARN.format("ecr", "repository/app/api"))
        self.assertEqual(scan["iac_changes"], [{"stack": "app", "logical_id": "Repo", "property": "ImageScanningConfiguration",
                                                "current": "{ ScanOnPush: false }", "proposed": "{ ScanOnPush: true }", "evidence_id": "s0001",
                                                "note": scan["iac_changes"][0]["note"]}])
        self.assertIn("s0001", scan["evidence_ids"])
        tag = self.change("ECR_TAG_IMMUTABILITY", ARN.format("ecr", "repository/app/api"))["iac_changes"][0]
        self.assertEqual((tag["property"], tag["current"], tag["proposed"]), ("ImageTagMutability", None, "IMMUTABLE"))
        retention = self.change("LOG_RETENTION", rid("logs.group", "/app/worker"))["iac_changes"][0]
        self.assertEqual((retention["logical_id"], retention["current"], retention["proposed"]), ("WorkerLogs", None, "30"))
        self.assertIn("example", retention["note"])
        self.assertEqual(self.change("RDS_BACKUPS", rid("rds.cluster", "c1"))["iac_changes"][0]["current"], "1")
        protection = self.change("RDS_DELETION_PROTECTION", rid("rds.cluster", "c1"))["iac_changes"]
        self.assertEqual([(c["property"], c["current"], c["proposed"]) for c in protection],
                         [("DeletionProtection", "false", "true"), ("DeletionPolicy", "Delete", "Snapshot")])
        insights = self.change("ECS_CONTAINER_INSIGHTS", ARN.format("ecs", "cluster/main"))["iac_changes"][0]
        self.assertEqual((insights["logical_id"], insights["current"]), ("Main", "- Name: containerInsights"))

    def test_json_template_values_are_dumped_with_sorted_keys(self):
        tag = self.change("ECR_TAG_IMMUTABILITY", ARN.format("ecr", "repository/json/api"))["iac_changes"][0]
        self.assertEqual((tag["stack"], tag["logical_id"], tag["current"]), ("json-stack", "JsonRepo", '"MUTABLE"'))
        scan = self.change("ECR_SCAN_ON_PUSH", ARN.format("ecr", "repository/json/api"))["iac_changes"][0]
        self.assertEqual((scan["current"], scan["evidence_id"]), ('{"ScanOnPush": false}', "s0003"))

    def test_changes_the_validator_rejects_are_dropped_without_error(self):
        # Subscription is set below Properties (not where the generator reads), so validation fails and nothing is attached.
        alarm = self.change("ALARM_NOTIFICATION_TARGET", TOPIC)
        self.assertEqual(alarm["status"], "FAIL")
        self.assertNotIn("iac_changes", alarm)
        self.assertNotIn("s0001", alarm["evidence_ids"])
        # A JSON template stored as text is a shape the YAML reader does not understand.
        self.assertNotIn("iac_changes", self.change("LOG_RETENTION", rid("logs.group", "/text/logs")))
        # Rules without a one-property fix get none.
        self.assertNotIn("iac_changes", self.change("AURORA_SINGLE_INSTANCE", rid("rds.cluster", "c1")))
        self.assertFalse([c for c in self.checks if c.get("iac_changes") and c["status"] not in ("FAIL", "WARN")])
        review.validate_iac_changes(self.checks, self.root, self.calls)

    def test_a_manual_replacement_keeps_its_own_changes(self):
        base = self.change("RDS_BACKUPS", rid("rds.cluster", "c1"))
        own = [dict(base["iac_changes"][0], proposed="14", note="owner policy")]
        merged = review.merge_manual(self.checks, [dict(base, id="m0001", iac_changes=own)])
        self.assertEqual(next(c for c in merged if c["id"] == "m0001")["iac_changes"], own)
        self.assertNotIn(base["id"], {c["id"] for c in merged})

    def test_cli_reads_the_run_scope_next_to_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            write_snapshot(run / "03-as-is-enriched", *workload())
            script = SCRIPTS / "review.py"

            def findings(*extra):
                result = run_python(script, "--as-is", run / "03-as-is-enriched", "--output", run / "04-review", *extra, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads((run / "04-review/findings.json").read_text())

            (run / "scope.json").write_text(json.dumps({"account_id": ACCOUNT, "workload_name": "orders", "context": {"environment": "non-production"}}))
            data = findings()
            self.assertEqual(data["ruleset_version"], "moda-starter-1.3")
            self.assertEqual(data["scope"]["environment"], "non-production")
            single = next(c for c in data["checks"] if c["rule_id"] == "AURORA_SINGLE_INSTANCE")
            self.assertEqual((single["severity"], single["severity_basis"]), ("MEDIUM", review.NON_PRODUCTION_BASIS))
            (run / "prod.json").write_text(json.dumps({"account_id": ACCOUNT, "context": {"environment": "production"}}))
            data = findings("--run-scope", run / "prod.json")
            self.assertEqual(next(c for c in data["checks"] if c["rule_id"] == "AURORA_SINGLE_INSTANCE")["severity"], "HIGH")
            (run / "scope.json").write_text(json.dumps({"account_id": "222222222222", "context": {"environment": "non-production"}}))
            result = run_python(script, "--as-is", run / "03-as-is-enriched", "--output", run / "04-review", capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("another account", result.stderr)


if __name__ == "__main__":
    unittest.main()
