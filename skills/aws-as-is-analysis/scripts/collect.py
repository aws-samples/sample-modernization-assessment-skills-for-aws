#!/usr/bin/env python3
"""Bounded, metadata-only AWS CLI v2 inventory collector. Python stdlib only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

# Each base entry: operation, resource type, JSON path, native ID, display name.
BASE = {
    "ec2": [
        ("describe-vpcs", "ec2.vpc", "Vpcs", "VpcId", "VpcId"),
        ("describe-subnets", "ec2.subnet", "Subnets", "SubnetId", "SubnetId"),
        ("describe-route-tables", "ec2.route-table", "RouteTables", "RouteTableId", "RouteTableId"),
        ("describe-internet-gateways", "ec2.internet-gateway", "InternetGateways", "InternetGatewayId", "InternetGatewayId"),
        ("describe-nat-gateways", "ec2.nat-gateway", "NatGateways", "NatGatewayId", "NatGatewayId"),
        ("describe-security-groups", "ec2.security-group", "SecurityGroups", "GroupId", "GroupName"),
        ("describe-instances", "ec2.instance", "Reservations.Instances", "InstanceId", "InstanceId"),
        ("describe-volumes", "ec2.volume", "Volumes", "VolumeId", "VolumeId"),
        ("describe-vpc-endpoints", "ec2.vpc-endpoint", "VpcEndpoints", "VpcEndpointId", "ServiceName"),
        # Network edge and hygiene (one cheap call each): peering, TGW, VPN, flow logs, Elastic IPs.
        ("describe-vpc-peering-connections", "ec2.vpc-peering", "VpcPeeringConnections", "VpcPeeringConnectionId", "VpcPeeringConnectionId"),
        ("describe-transit-gateways", "ec2.transit-gateway", "TransitGateways", "TransitGatewayId", "TransitGatewayId"),
        ("describe-vpn-connections", "ec2.vpn-connection", "VpnConnections", "VpnConnectionId", "VpnConnectionId"),
        ("describe-flow-logs", "ec2.flow-log", "FlowLogs", "FlowLogId", "FlowLogId"),
        ("describe-addresses", "ec2.elastic-ip", "Addresses", "AllocationId", "PublicIp"),
    ],
    # Previous-generation and platform signals: Classic Load Balancers, Auto Scaling groups and launch
    # configurations, SSM-managed instances (OS name and version without touching the host).
    "elb": [("describe-load-balancers", "elb.classic-load-balancer", "LoadBalancerDescriptions", "LoadBalancerName", "DNSName")],
    "autoscaling": [
        ("describe-auto-scaling-groups", "autoscaling.group", "AutoScalingGroups", "AutoScalingGroupARN", "AutoScalingGroupName"),
        ("describe-launch-configurations", "autoscaling.launch-configuration", "LaunchConfigurations", "LaunchConfigurationARN", "LaunchConfigurationName"),
    ],
    "ssm": [("describe-instance-information", "ssm.managed-instance", "InstanceInformationList", "InstanceId", "ComputerName")],
    # Network edge: certificates, API gateways, regional Web ACLs, Direct Connect.
    "acm": [("list-certificates", "acm.certificate", "CertificateSummaryList", "CertificateArn", "DomainName")],
    "apigateway": [("get-rest-apis", "apigateway.rest-api", "items", "id", "name")],
    "apigatewayv2": [("get-apis", "apigatewayv2.api", "Items", "ApiId", "Name")],
    "wafv2": [("list-web-acls", "wafv2.web-acl", "WebACLs", "ARN", "Name", ("--scope", "REGIONAL"))],
    "directconnect": [("describe-connections", "directconnect.connection", "connections", "connectionId", "connectionName")],
    "rds": [
        ("describe-db-instances", "rds.instance", "DBInstances", "DBInstanceIdentifier", "DBInstanceIdentifier"),
        ("describe-db-clusters", "rds.cluster", "DBClusters", "DBClusterIdentifier", "DBClusterIdentifier"),
    ],
    "elbv2": [
        ("describe-load-balancers", "elbv2.load-balancer", "LoadBalancers", "LoadBalancerArn", "LoadBalancerName"),
        ("describe-target-groups", "elbv2.target-group", "TargetGroups", "TargetGroupArn", "TargetGroupName"),
    ],
    "lambda": [("list-functions", "lambda.function", "Functions", "FunctionArn", "FunctionName")],
    "cloudwatch": [("describe-alarms", "cloudwatch.alarm", "MetricAlarms", "AlarmArn", "AlarmName")],
    "redshift": [("describe-clusters", "redshift.cluster", "Clusters", "ClusterIdentifier", "ClusterIdentifier")],
}
# Optional platform services; not added to the default account scan.
# listing, list path, native key, display key, type, detail op, argument, detail path
PLATFORM_CONFIG = {
    "amp": ("list-workspaces", "workspaces", "workspaceId", "alias", "amp.workspace",
            "describe-workspace", "--workspace-id", "workspace"),
    "grafana": ("list-workspaces", "workspaces", "id", "name", "grafana.workspace",
                "describe-workspace", "--workspace-id", "workspace"),
    "kafka": ("list-clusters-v2", "ClusterInfoList", "ClusterArn", "ClusterName", "kafka.cluster",
              "describe-cluster-v2", "--cluster-arn", "ClusterInfo"),
}
# Global services are collected once; the support, health and organizations APIs answer only in us-east-1.
GLOBAL_REGION = {"s3": None, "iam": None, "route53": "us-east-1", "cloudfront": "us-east-1",
                 "support": "us-east-1", "health": "us-east-1", "organizations": "us-east-1", "ce": "us-east-1"}
# Inventory-only listings (one call per region) enabled when discovery finds the service or the scope names it.
# operation, type, list path (None = top-level array of strings), native key, display key[, extra arguments]
OPTIONAL = {
    "route53": [("list-hosted-zones", "route53.hosted-zone", "HostedZones", "Id", "Name")],
    "cloudfront": [("list-distributions", "cloudfront.distribution", "DistributionList.Items", "ARN", "DomainName")],
    "elasticache": [("describe-cache-clusters", "elasticache.cluster", "CacheClusters", "CacheClusterId", "CacheClusterId", ("--show-cache-node-info",)),
                    ("describe-replication-groups", "elasticache.replication-group", "ReplicationGroups", "ReplicationGroupId", "ReplicationGroupId")],
    "ecr": [("describe-repositories", "ecr.repository", "repositories", "repositoryArn", "repositoryName")],
    "efs": [("describe-file-systems", "efs.file-system", "FileSystems", "FileSystemId", "Name")],
    "fsx": [("describe-file-systems", "fsx.file-system", "FileSystems", "FileSystemId", "FileSystemId")],
    "backup": [("list-backup-plans", "backup.plan", "BackupPlansList", "BackupPlanId", "BackupPlanName"),
               ("list-backup-vaults", "backup.vault", "BackupVaultList", "BackupVaultName", "BackupVaultName")],
    "kms": [("list-aliases", "kms.alias", "Aliases", "AliasArn", "AliasName")],
    "secretsmanager": [("list-secrets", "secretsmanager.secret", "SecretList", "ARN", "Name")],
    "elasticbeanstalk": [("describe-environments", "elasticbeanstalk.environment", "Environments", "EnvironmentId", "EnvironmentName")],
    "codepipeline": [("list-pipelines", "codepipeline.pipeline", "pipelines", "name", "name")],
    "codebuild": [("list-projects", "codebuild.project", "projects", None, None)],
    "codecommit": [("list-repositories", "codecommit.repository", "repositories", "repositoryId", "repositoryName")],
    "stepfunctions": [("list-state-machines", "stepfunctions.state-machine", "stateMachines", "stateMachineArn", "name")],
    "cognito-idp": [("list-user-pools", "cognito.user-pool", "UserPools", "Id", "Name", ("--max-results", "60"))],
    "appsync": [("list-graphql-apis", "appsync.api", "graphqlApis", "apiId", "name")],
    "mq": [("list-brokers", "mq.broker", "BrokerSummaries", "BrokerId", "BrokerName")],
    "kinesis": [("list-streams", "kinesis.stream", "StreamSummaries", "StreamARN", "StreamName")],
    "firehose": [("list-delivery-streams", "firehose.delivery-stream", "DeliveryStreamNames", None, None)],
    "glue": [("get-databases", "glue.database", "DatabaseList", "Name", "Name"),
             ("get-jobs", "glue.job", "Jobs", "Name", "Name")],
    "athena": [("list-work-groups", "athena.workgroup", "WorkGroups", "Name", "Name")],
    "emr": [("list-clusters", "emr.cluster", "Clusters", "Id", "Name", ("--active",))],
    "sagemaker": [("list-endpoints", "sagemaker.endpoint", "Endpoints", "EndpointArn", "EndpointName"),
                  ("list-notebook-instances", "sagemaker.notebook", "NotebookInstances", "NotebookInstanceArn", "NotebookInstanceName")],
    "bedrock": [("list-guardrails", "bedrock.guardrail", "guardrails", "id", "name"),
                ("list-custom-models", "bedrock.custom-model", "modelSummaries", "modelArn", "modelName")],
    "bedrock-agent": [("list-agents", "bedrock.agent", "agentSummaries", "agentId", "agentName"),
                      ("list-knowledge-bases", "bedrock.knowledge-base", "knowledgeBaseSummaries", "knowledgeBaseId", "name")],
    "personalize": [("list-campaigns", "personalize.campaign", "campaigns", "campaignArn", "name")],
    "kendra": [("list-indices", "kendra.index", "IndexConfigurationSummaryItems", "Id", "Name")],
    "lexv2-models": [("list-bots", "lex.bot", "botSummaries", "botId", "botName")],
    "connect": [("list-instances", "connect.instance", "InstanceSummaryList", "Id", "InstanceAlias")],
    "servicediscovery": [("list-namespaces", "servicediscovery.namespace", "Namespaces", "Id", "Name")],
    "appmesh": [("list-meshes", "appmesh.mesh", "meshes", "arn", "meshName")],
    "vpc-lattice": [("list-service-networks", "vpc-lattice.service-network", "items", "id", "name")],
    "transfer": [("list-servers", "transfer.server", "Servers", "ServerId", "ServerId")],
    "mgn": [("describe-source-servers", "mgn.source-server", "items", "sourceServerID", "sourceServerID")],
    "dms": [("describe-replication-instances", "dms.replication-instance", "ReplicationInstances", "ReplicationInstanceArn", "ReplicationInstanceIdentifier"),
            ("describe-replication-tasks", "dms.replication-task", "ReplicationTasks", "ReplicationTaskArn", "ReplicationTaskIdentifier")],
    "iot": [("list-things", "iot.thing", "things", "thingArn", "thingName", ("--max-results", "100"))],
    "license-manager": [("list-license-configurations", "license-manager.configuration", "LicenseConfigurations", "LicenseConfigurationArn", "Name")],
    "quicksight": [("list-dashboards", "quicksight.dashboard", "DashboardSummaryList", "Arn", "Name", ("--aws-account-id", "{account}")),
                   ("list-data-sets", "quicksight.dataset", "DataSetSummaries", "Arn", "Name", ("--aws-account-id", "{account}"))],
}
# Account posture and AWS-computed recommendations (read-only; several answer "not enabled" or need a support plan).
ACCOUNT_SERVICES = ("securityhub", "guardduty", "config", "inspector2", "accessanalyzer", "computeoptimizer",
                    "support", "health", "organizations", "ce")
# Internal service name -> AWS CLI command name where they differ.
CLI_NAME = {"s3": "s3api", "config": "configservice", "computeoptimizer": "compute-optimizer", "tagging": "resourcegroupstaggingapi"}
# ARN service name -> collector service name (identity when absent). Used by discovery to enable optional listings.
ARN_SERVICE = {"logs": "cloudwatch", "es": "opensearch", "aps": "amp", "elasticfilesystem": "efs", "elasticmapreduce": "emr",
               "states": "stepfunctions", "lex": "lexv2-models", "cognito-idp": "cognito-idp", "elasticloadbalancing": "elbv2",
               "bedrock": "bedrock", "config": "config", "securityhub": "securityhub", "guardduty": "guardduty",
               "access-analyzer": "accessanalyzer", "compute-optimizer": "computeoptimizer"}
# Resource types handled per workload by supplement.py when they belong to an in-scope stack.
SUPPLEMENT_SERVICES = {"elasticache", "ecr", "codepipeline", "codebuild", "codecommit", "secretsmanager", "backup", "cloudformation",
                       "elasticfilesystem", "logs", "sns", "events", "iam", "ssm", "application-autoscaling", "wafv2", "bedrock"}
# Default scan: eleven workload services plus the cheap platform and edge listings. Discovery is always on.
DEFAULT_SERVICES = ("ec2", "elb", "elbv2", "autoscaling", "rds", "ecs", "eks", "lambda", "s3", "dynamodb", "cloudwatch",
                    "cloudtrail", "iam", "ssm", "acm", "apigateway", "apigatewayv2", "wafv2", "directconnect",
                    "route53", "cloudfront", *ACCOUNT_SERVICES)
ACCOUNT_LEVEL_TYPES_PREFIX = ("iam.account-summary", "s3.account-public-access-block", "cloudtrail.trail", "discovery.",
                              "securityhub.", "guardduty.", "config.", "inspector2.", "accessanalyzer.", "computeoptimizer.",
                              "support.", "health.", "organizations.", "controltower.", "ce.")
SUPPORTED = tuple(BASE) + ("ecs", "eks", "s3", "dynamodb", "cloudtrail", "iam", "opensearch",
                           *PLATFORM_CONFIG, "sqs", "sns", "events", *OPTIONAL, *ACCOUNT_SERVICES)
DETAIL_OPERATIONS = {
    ("sts", "get-caller-identity"),
    ("elbv2", "describe-listeners"), ("elbv2", "describe-target-health"),
    ("ecs", "list-clusters"), ("ecs", "describe-clusters"),
    ("ecs", "list-services"), ("ecs", "describe-services"),
    ("eks", "list-clusters"), ("eks", "describe-cluster"),
    ("s3", "list-buckets"), ("s3", "get-bucket-location"),
    ("s3", "get-public-access-block"), ("s3", "get-bucket-encryption"),
    ("s3", "get-bucket-versioning"), ("s3control", "get-public-access-block"),
    ("dynamodb", "list-tables"), ("dynamodb", "describe-table"),
    ("dynamodb", "describe-continuous-backups"),
    ("cloudtrail", "describe-trails"), ("cloudtrail", "get-trail-status"),
    ("logs", "describe-log-groups"), ("iam", "get-account-summary"),
    ("opensearch", "list-domain-names"), ("opensearch", "describe-domain"),
    ("sqs", "list-queues"), ("sqs", "get-queue-attributes"),
    ("sns", "list-topics"), ("sns", "get-topic-attributes"),
    ("events", "list-event-buses"), ("events", "describe-event-bus"),
    ("events", "list-rules"), ("events", "list-targets-by-rule"),
    ("tagging", "get-resources"), ("wafv2", "list-web-acls"),
    ("route53", "list-resource-record-sets"), ("cloudfront", "get-distribution-config"),
    ("securityhub", "describe-hub"), ("securityhub", "get-enabled-standards"),
    ("guardduty", "list-detectors"), ("guardduty", "get-detector"),
    ("config", "describe-configuration-recorders"), ("config", "describe-configuration-recorder-status"),
    ("config", "describe-delivery-channels"), ("config", "get-discovered-resource-counts"), ("config", "describe-config-rules"),
    ("inspector2", "batch-get-account-status"),
    ("accessanalyzer", "list-analyzers"), ("accessanalyzer", "list-findings-v2"),
    ("ce", "get-cost-and-usage"),
    ("computeoptimizer", "get-enrollment-status"), ("computeoptimizer", "get-recommendation-summaries"),
    ("computeoptimizer", "get-ec2-instance-recommendations"), ("computeoptimizer", "get-auto-scaling-group-recommendations"),
    ("support", "describe-trusted-advisor-checks"), ("support", "describe-trusted-advisor-check-summaries"),
    ("health", "describe-events"), ("organizations", "describe-organization"), ("controltower", "list-landing-zones"),
}
DETAIL_OPERATIONS |= {(s, spec[i]) for s, spec in PLATFORM_CONFIG.items() for i in (0, 5)}
ALLOWED = DETAIL_OPERATIONS | {(service, item[0]) for service, items in BASE.items() for item in items}
ALLOWED |= {(service, item[0]) for service, items in OPTIONAL.items() for item in items}
TAGGING_QUERY = "ResourceTagMappingList[].{ARN:ResourceARN,Keys:Tags[].Key}"
HEALTH_QUERY = ("events[].{arn:arn,service:service,eventTypeCode:eventTypeCode,eventTypeCategory:eventTypeCategory,"
                "region:region,startTime:startTime,endTime:endTime,statusCode:statusCode,eventScopeCode:eventScopeCode}")
ACCESS_FINDINGS_QUERY = "findings[].{id:id,resourceType:resourceType,resource:resource,findingType:findingType,status:status,createdAt:createdAt}"
GLUE_JOBS_QUERY = "Jobs[].{Name:Name,Role:Role,GlueVersion:GlueVersion,WorkerType:WorkerType,Command:Command.Name,CreatedOn:CreatedOn}"
EC2_RECOMMENDATION_QUERY = ("instanceRecommendations[].{instanceArn:instanceArn,currentInstanceType:currentInstanceType,finding:finding,"
                            "findingReasonCodes:findingReasonCodes,recommendationOptions:recommendationOptions[].{instanceType:instanceType,"
                            "rank:rank,performanceRisk:performanceRisk,migrationEffort:migrationEffort},"
                            "currentPerformanceRisk:currentPerformanceRisk,effectiveRecommendationPreferences:effectiveRecommendationPreferences.cpuVendorArchitectures}")
# Error codes meaning "this account has not enabled the service or lacks the support plan", recorded as not_enabled
# instead of error so the report can say so without lowering the collection status.
NOT_ENABLED = {"SubscriptionRequiredException", "InvalidAccessException", "AWSOrganizationsNotInUseException",
               "OptInRequired", "OptInRequiredException", "NoSuchEntityException", "DataUnavailableException"}
LAMBDA_QUERY = (
    "Functions[].{FunctionName:FunctionName,FunctionArn:FunctionArn,Runtime:Runtime,"
    "MemorySize:MemorySize,Timeout:Timeout,PackageType:PackageType,"
    "Architectures:Architectures,VpcConfig:VpcConfig,Role:Role,LastModified:LastModified}"
)
EVENT_TARGET_QUERY = (
    "Targets[].{Id:Id,Arn:Arn,RoleArn:RoleArn,RetryPolicy:RetryPolicy,DeadLetterConfig:DeadLetterConfig}"
)
AUTH_FATAL = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
              "UnrecognizedClientException", "SignatureDoesNotMatch"}


def account_level(kind):
    return kind.startswith(ACCOUNT_LEVEL_TYPES_PREFIX)


def parse_arn(arn):
    """(service, resource type) from an ARN; the type is the prefix before '/' or ':' in the resource part."""
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return None, None
    service, resource = parts[2], parts[5]
    if service == "s3":
        return "s3", "bucket"
    for sep in ("/", ":"):
        if sep in resource:
            head = resource.split(sep, 1)[0]
            return service, head if head else resource
    return service, resource


def now():
    return datetime.now(timezone.utc).isoformat()


SKILL_DIR = Path(__file__).resolve().parent.parent


def find_run_dir(path):
    """The run directory above an output path: the nearest folder that holds 01-setup/access-check.json."""
    for folder in [Path(path).resolve(), *Path(path).resolve().parents][:5]:
        if (folder / "01-setup/access-check.json").is_file():
            return folder
    return None


def assessment_gate(output):
    """None when this run may call AWS, else the reason it may not.

    Stage 1 records in 01-setup/access-check.json whether the profile can change the account (`write_capability`).
    `yes` or `unknown` needs the person's decision recorded in scope.json (`write_capable_acknowledged`); until then the
    helpers that call AWS refuse to start. A run without access-check.json (a helper used on its own) is not gated."""
    run = find_run_dir(output)
    if run is None:
        return None
    try:
        access = json.loads((run / "01-setup/access-check.json").read_text(encoding="utf-8"))
        scope_file = run / "scope.json"
        scope = json.loads(scope_file.read_text(encoding="utf-8")) if scope_file.is_file() else {}
    except (OSError, ValueError):
        return "access_check_unreadable"
    status = (access.get("write_capability") or {}).get("status", "unknown")
    if status == "no":
        return None
    acknowledged = scope.get("write_capable_acknowledged")
    if isinstance(acknowledged, dict) and str(acknowledged.get("reason") or "").strip():
        return None
    return f"write_capable_profile_not_acknowledged (write_capability={status}; switch to a read-only profile or record the person's decision in scope.json)"


def verify_skill_files(skill_dir=None):
    """None when this skill's files match the install manifest, else the first file that differs.

    The manifest (aws-skills-install/assets/install-manifest.json next to this skill) records every file's SHA-256, so
    a helper edited after installation, for example to add a call outside its allowlist, refuses to run."""
    skill_dir = Path(skill_dir or SKILL_DIR)
    manifest_path = skill_dir.parent / "aws-skills-install/assets/install-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "install_manifest_unreadable"
    entry = next((s for s in manifest.get("skills", []) if s.get("name") == skill_dir.name), None)
    if entry is None:
        return None
    for item in entry.get("files", []):
        file = skill_dir / item["path"]
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != item.get("sha256"):
            return f"skill_file_modified ({skill_dir.name}/{item['path']}); reinstall the skills"
    return None


def checked_arguments(arguments):
    """Guard for every CLI invocation: list form only (no shell), the program name is a literal at the call site
    (`["aws", *checked_arguments(...)]`, resolved through PATH exactly as shutil.which resolved it), and every argument
    is a string without NUL or newline. Arguments come from our own tables, resource identifiers read from AWS
    responses, or validated CLI options."""
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("command arguments must be a non-empty list")
    for item in arguments:
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("command arguments must be strings without control characters")
    return list(arguments)



def environment(profile):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AWS_ENDPOINT_URL"):
            env.pop(key)
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off",
               AWS_EC2_METADATA_DISABLED="true", AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true",
               AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="4")
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_DEFAULT_PROFILE", None)
    if profile:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
            env.pop(key, None)
    else:
        if not env.get("AWS_ACCESS_KEY_ID") or not env.get("AWS_SECRET_ACCESS_KEY"):
            raise ValueError("environment credentials missing")
        if env["AWS_ACCESS_KEY_ID"].startswith("ASIA") and not env.get("AWS_SESSION_TOKEN"):
            raise ValueError("temporary session token missing")
        env.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull)
        for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
            env.pop(key, None)
    return env


def select(data, path):
    if not path:
        return data if isinstance(data, list) else [data]
    if isinstance(data, list):
        return [item for value in data for item in select(value, path)]
    head, _, tail = path.partition(".")
    return select(data.get(head, []), tail) if isinstance(data, dict) else []


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def scrub(data):
    if isinstance(data, dict):
        return {key: "[REDACTED]" if re.search(
            r"(?i)(secretaccesskey|sessiontoken|password|authorizationtoken|environment)$", key
        ) else scrub(value) for key, value in data.items()}
    if isinstance(data, list):
        return [scrub(item) for item in data]
    return data


class Collector:
    def __init__(self, args):
        self.args = args
        self.env = environment(args.profile)
        self.aws = shutil.which("aws")
        if not self.aws:
            raise ValueError("AWS CLI v2 missing")
        version = subprocess.run(["aws", "--version"], capture_output=True, text=True, timeout=15)  # nosec B603 B607 - static argv
        if version.returncode or not (version.stdout + version.stderr).startswith("aws-cli/2."):
            raise ValueError("AWS CLI v2 required")
        self.root = args.output
        self.calls = []
        self.resources = {}
        self.truncations = []
        self.used = 0
        self.fatal = False
        self.partition = "aws"
        self.started = now()
        # Concurrency: service/region units and per-resource detail calls run in threads; the semaphore caps
        # simultaneous AWS CLI processes at max_workers no matter how the threads nest.
        self.workers = max(1, getattr(args, "max_workers", 8))
        self.slots = threading.BoundedSemaphore(self.workers)
        self.lock = threading.RLock()
        self.unit = threading.local()
        # Budget fairness: half of max_api_calls is reserved in equal shares per requested service, the other
        # half is shared first come. One large service can no longer starve the services after it.
        self.used_by = {}
        self.done = set()
        self.pending = {}
        self.discovery = {}
        self.auto_enabled = []
        self.set_services(list(dict.fromkeys(getattr(args, "services", []) or [])))

    def set_services(self, services):
        self.args.services = services
        self.share = self.args.max_api_calls // (2 * len(services)) if services else 0
        self.shared_capacity = self.args.max_api_calls - self.share * len(services)

    def reserve(self):
        """Return None if a call may run (and count it), else the skip reason. Caller holds the lock."""
        if self.used >= self.args.max_api_calls:
            return "api_budget_exhausted"
        unit = getattr(self.unit, "name", None)
        used = self.used_by.get(unit, 0)
        if unit is None or used >= self.share:
            reserved_used = sum(min(v, self.share) for k, v in self.used_by.items() if k is not None)
            # A finished service hands back the part of its share it did not use.
            released = sum(self.share - min(self.used_by.get(k, 0), self.share) for k in self.done)
            if self.used - reserved_used >= self.shared_capacity + released:
                return "api_budget_exhausted"
        self.used += 1
        self.used_by[unit] = used + 1
        return None

    def each(self, function, items):
        """Map function over items in threads that inherit the current budget unit."""
        items = list(items)
        if self.workers == 1 or len(items) < 2:
            return [function(item) for item in items]
        unit = getattr(self.unit, "name", None)

        def run(item):
            self.unit.name = unit
            return function(item)
        with ThreadPoolExecutor(max_workers=min(self.workers, len(items))) as pool:
            return list(pool.map(run, items))

    def call(self, service, operation, region, *arguments, query=None):
        if (service, operation) not in ALLOWED:
            raise ValueError("operation not in metadata allowlist")
        with self.lock:
            record = {"id": f"e{len(self.calls) + 1:04d}", "service": service, "operation": operation,
                      "region": region, "arguments": list(arguments), "collected_at": now(),
                      "raw_path": None, "sha256": None, "error_code": None}
            self.calls.append(record)
            skip = "auth_failed" if self.fatal else self.reserve()
            if skip:
                record.update(status="skipped", error_code=skip)
                return None, record
        cli_service = CLI_NAME.get(service, service)
        argv = [cli_service, operation, *arguments, "--region", region, "--output", "json", "--no-cli-pager"]
        if self.args.profile:
            argv += ["--profile", self.args.profile]
        if query:
            argv += ["--query", query]
            record["query"] = query
        try:
            with self.slots:
                response = subprocess.run(["aws", *checked_arguments(argv)], capture_output=True, text=True,  # nosec B603 B607 - see checked_arguments
                                          env=self.env, timeout=self.args.timeout)
            if response.returncode:
                match = re.search(r"\(([A-Za-z0-9_.-]+)\)", response.stderr)
                code = match.group(1) if match else "cli_or_network_error"
                status = "error"
                if code in AUTH_FATAL:
                    status, self.fatal = "expired_token", True
                elif code in NOT_ENABLED or "not subscribed" in response.stderr or "is not enabled" in response.stderr:
                    status = "not_enabled"
                elif "AccessDenied" in code or "Unauthorized" in code:
                    status = "access_denied"
                elif "Throttl" in code or code in {"RequestLimitExceeded", "TooManyRequestsException"}:
                    status = "throttled"
                elif any(word in code for word in ("NotFound", "NoSuch", "NotExist")):
                    status = "not_found"
                record.update(status=status, error_code=code)
                return None, record
            # CLI's JSON formatter can print nothing for the empty versioning
            # configuration of a bucket that has never enabled versioning.
            if not response.stdout.strip() and (service, operation) == ("s3", "get-bucket-versioning"):
                data = {}
            else:
                data = scrub(json.loads(response.stdout))
            record["status"] = "ok"
            if isinstance(data, dict) and data.get("failures"):
                record.update(status="error", error_code="partial_service_response")
            record["raw_path"] = f"raw/{record['id']}.json"
            write_json(self.root / record["raw_path"], data)
            record["sha256"] = hashlib.sha256((self.root / record["raw_path"]).read_bytes()).hexdigest()
            return data, record
        except subprocess.TimeoutExpired:
            record.update(status="error", error_code="timeout")
        except (json.JSONDecodeError, OSError):
            record.update(status="error", error_code="invalid_response_or_io_error")
        return None, record

    def add(self, kind, region, native_id, attributes, record, name=None):
        with self.lock:
            return self._add(kind, region, native_id, attributes, record, name)

    def _add(self, kind, region, native_id, attributes, record, name=None):
        rid = f"{self.args.expected_account}:{region}:{kind}:{native_id}"
        if rid not in self.resources:
            self.resources[rid] = {
                "id": rid, "type": kind, "native_id": str(native_id),
                "name": str(name or native_id), "account_id": self.args.expected_account,
                "region": region, "attributes": attributes or {}, "evidence_ids": [],
                "observations": {}, "level": "account" if account_level(kind) else "workload",
            }
        resource = self.resources[rid]
        if record["id"] not in resource["evidence_ids"]:
            resource["evidence_ids"].append(record["id"])
        return resource

    def observe(self, resource, label, service, operation, region, *arguments, query=None):
        data, record = self.call(service, operation, region, *arguments, query=query)
        with self.lock:
            resource["observations"][label] = {"status": record["status"], "data": data,
                                                "error_code": record["error_code"], "evidence_id": record["id"]}
            resource["evidence_ids"].append(record["id"])
        return data

    def limited(self, values, label):
        if len(values) > self.args.max_detail_resources:
            with self.lock:
                self.truncations.append({"scope": label, "discovered": len(values),
                                         "detailed": self.args.max_detail_resources,
                                         "reason": "detail_resource_limit"})
        return values[:self.args.max_detail_resources]

    def listing(self, service, spec, region):
        operation, kind, path, key, name = spec[:5]
        extra = tuple(x.replace("{account}", self.args.expected_account) for x in spec[5]) if len(spec) > 5 else ()
        query = {"lambda": LAMBDA_QUERY}.get(service) if service in BASE else ({("glue", "get-jobs"): GLUE_JOBS_QUERY}.get((service, operation)))
        data, record = self.call(service, operation, region, *extra, query=query)
        if data is None:
            return record, []
        values = data if query else select(data, path)
        record["item_count"] = len(values)
        if key is None:  # top-level array of names
            added = [self.add(kind, region, v, {"Name": v}, record) for v in values if isinstance(v, str)]
        else:
            added = [self.add(kind, region, v[key], v, record, v.get(name)) for v in values if isinstance(v, dict) and key in v]
        return record, added

    def optional(self, service, region):
        for spec in OPTIONAL[service]:
            record, added = self.listing(service, spec, region)
            if spec[1] == "route53.hosted-zone":
                self.each(lambda z: self.observe(z, "record_sets", "route53", "list-resource-record-sets", region,
                                                 "--hosted-zone-id", z["native_id"].rsplit("/", 1)[-1]),
                          self.limited(added, "route53:record-sets"))
            elif spec[1] == "cloudfront.distribution":
                self.each(lambda d: self.observe(d, "configuration", "cloudfront", "get-distribution-config", region,
                                                 "--id", d["attributes"].get("Id", "")),
                          self.limited(added, "cloudfront:distribution-config"))

    def base(self, service, region):
        for spec in BASE[service]:
            kind = spec[1]
            record, added = self.listing(service, spec, region)
            if record["status"] != "ok":
                continue
            if kind == "elbv2.load-balancer":
                self.each(lambda r: self.observe(r, "listeners", "elbv2", "describe-listeners", region,
                                                 "--load-balancer-arn", r["native_id"]),
                          self.limited(added, f"{region}:listeners"))
            elif kind == "elbv2.target-group":
                self.each(lambda r: self.observe(r, "target_health", "elbv2", "describe-target-health", region,
                                                 "--target-group-arn", r["native_id"]),
                          self.limited(added, f"{region}:targets"))
        if service == "cloudwatch":
            data, record = self.call("logs", "describe-log-groups", region)
            for value in select(data, "logGroups"):
                self.add("logs.group", region, value["logGroupName"], value, record)
        if service == "wafv2" and region == self.args.regions[0] and self.partition == "aws":
            data, record = self.call("wafv2", "list-web-acls", "us-east-1", "--scope", "CLOUDFRONT")
            for value in select(data, "WebACLs"):
                self.add("wafv2.web-acl", "global", value["ARN"], dict(value, Scope="CLOUDFRONT"), record, value.get("Name"))

    def clusters(self, service, region):
        data, record = self.call(service, "list-clusters", region)
        identifiers = select(data, "clusterArns" if service == "ecs" else "clusters")
        record["item_count"] = len(identifiers)
        # Preserve discovered identities even if detail collection is limited or denied.
        resources = [self.add(f"{service}.cluster", region, identifier, {}, record) for identifier in identifiers]
        self.each(lambda r: self.cluster_detail(service, region, r),
                  self.limited(resources, f"{region}:{service}:clusters"))

    def cluster_detail(self, service, region, resource):
        identifier = resource["native_id"]
        operation = "describe-clusters" if service == "ecs" else "describe-cluster"
        argument = "--clusters" if service == "ecs" else "--name"
        extra = ("--include", "SETTINGS") if service == "ecs" else ()
        detail = self.observe(resource, "configuration", service, operation, region, argument, identifier, *extra)
        candidates = select(detail, "clusters" if service == "ecs" else "cluster")
        if candidates:
            resource["attributes"] = candidates[0]
            resource["name"] = candidates[0].get("clusterName", candidates[0].get("name", identifier))
        if service == "ecs":
            services, listing = self.call("ecs", "list-services", region, "--cluster", identifier)
            service_ids = select(services, "serviceArns")
            listing["item_count"] = len(service_ids)
            def describe(arn):
                result, evidence = self.call("ecs", "describe-services", region,
                                             "--cluster", identifier, "--services", arn)
                for value in select(result, "services"):
                    self.add("ecs.service", region, value["serviceArn"], value, evidence, value.get("serviceName"))
            self.each(describe, self.limited(service_ids, f"{region}:ecs:services:{identifier}"))

    def s3(self, region):
        data, record = self.call("s3", "list-buckets", region)
        buckets = select(data, "Buckets")
        record["item_count"] = len(buckets)
        account, account_record = self.call("s3control", "get-public-access-block", region,
                                            "--account-id", self.args.expected_account)
        self.add("s3.account-public-access-block", "global", self.args.expected_account,
                 account or {}, account_record)
        self.each(lambda b: self.bucket_detail(region, record, b), self.limited(buckets, "s3:bucket-details"))

    def bucket_detail(self, region, record, bucket):
        name = bucket["Name"]
        location, evidence = self.call("s3", "get-bucket-location", region, "--bucket", name)
        if evidence["status"] != "ok":
            self.add("s3.bucket", "unknown", name, bucket, evidence)
            return
        bucket_region = location.get("LocationConstraint") or "us-east-1"
        if bucket_region == "EU":
            bucket_region = "eu-west-1"
        if bucket_region not in self.args.regions:
            return
        resource = self.add("s3.bucket", bucket_region, name, bucket, record)
        with self.lock:
            resource["evidence_ids"].append(evidence["id"])
        for label, operation in (
            ("public_access_block", "get-public-access-block"),
            ("encryption", "get-bucket-encryption"), ("versioning", "get-bucket-versioning"),
        ):
            self.observe(resource, label, "s3", operation, bucket_region, "--bucket", name)

    def dynamodb(self, region):
        data, record = self.call("dynamodb", "list-tables", region)
        names = select(data, "TableNames")
        record["item_count"] = len(names)
        resources = [self.add("dynamodb.table", region, name, {"TableName": name}, record) for name in names]
        def describe(resource):
            detail = self.observe(resource, "configuration", "dynamodb", "describe-table", region,
                                  "--table-name", resource["native_id"])
            if detail:
                resource["attributes"] = detail.get("Table", {})
            self.observe(resource, "continuous_backups", "dynamodb", "describe-continuous-backups", region,
                         "--table-name", resource["native_id"])
        self.each(describe, self.limited(resources, f"{region}:dynamodb:tables"))

    def cloudtrail(self, region):
        data, record = self.call("cloudtrail", "describe-trails", region)
        trails = select(data, "trailList")
        for value in trails:
            trail_region = value.get("HomeRegion", region)
            resource = self.add("cloudtrail.trail", trail_region, value["TrailARN"], value, record, value.get("Name"))
            with self.lock:
                claim = trail_region in self.args.regions and "logging" not in resource["observations"]
                if claim:
                    resource["observations"]["logging"] = {"status": "pending"}
            if claim:
                self.observe(resource, "logging", "cloudtrail", "get-trail-status", trail_region,
                             "--name", value["TrailARN"])

    def opensearch(self, region):
        data, record = self.call("opensearch", "list-domain-names", region)
        names = [value["DomainName"] for value in select(data, "DomainNames")]
        for name in self.limited(names, f"{region}:opensearch:domains"):
            detail, evidence = self.call("opensearch", "describe-domain", region, "--domain-name", name)
            self.add("opensearch.domain", region, name, (detail or {}).get("DomainStatus", {}), evidence)

    def platform(self, service, region):
        listing, path, key, name, kind, detail_op, argument, detail_path = PLATFORM_CONFIG[service]
        data, record = self.call(service, listing, region)
        values = select(data, path)
        record["item_count"] = len(values)
        resources = [self.add(kind, region, value[key], value, record, value.get(name)) for value in values]
        def describe(resource):
            detail = self.observe(resource, "configuration", service, detail_op, region, argument, resource["native_id"])
            if isinstance(detail, dict) and isinstance(detail.get(detail_path), dict):
                resource["attributes"] = detail[detail_path]
        self.each(describe, self.limited(resources, f"{region}:{service}:details"))

    def sqs(self, region):
        # SQS needs a page size to return continuation tokens for >1,000 queues.
        data, record = self.call("sqs", "list-queues", region, "--page-size", "1000",
                                 query="{QueueUrls: QueueUrls || `[]`}")
        urls = select(data, "QueueUrls")
        record["item_count"] = len(urls)
        resources = [self.add("sqs.queue", region, url, {"QueueUrl": url}, record, url.rsplit("/", 1)[-1]) for url in urls]
        def describe(resource):
            detail = self.observe(resource, "configuration", "sqs", "get-queue-attributes", region,
                                  "--queue-url", resource["native_id"], "--attribute-names", "All")
            if isinstance(detail, dict):
                resource["attributes"].update(detail.get("Attributes", {}))
        self.each(describe, self.limited(resources, f"{region}:sqs:queues"))

    def sns(self, region):
        data, record = self.call("sns", "list-topics", region)
        topics = select(data, "Topics")
        record["item_count"] = len(topics)
        resources = [self.add("sns.topic", region, t["TopicArn"], t, record) for t in topics]
        def describe(resource):
            detail = self.observe(resource, "configuration", "sns", "get-topic-attributes", region,
                                  "--topic-arn", resource["native_id"])
            if isinstance(detail, dict):
                resource["attributes"].update(detail.get("Attributes", {}))
        self.each(describe, self.limited(resources, f"{region}:sns:topics"))

    def events(self, region):
        data, record = self.call("events", "list-event-buses", region)
        buses = select(data, "EventBuses")
        record["item_count"] = len(buses)
        resources = [self.add("events.bus", region, b["Name"], b, record) for b in buses]
        for bus in self.limited(resources, f"{region}:events:buses"):
            self.observe(bus, "configuration", "events", "describe-event-bus", region,
                         "--name", bus["native_id"])
            rules, evidence = self.call("events", "list-rules", region, "--event-bus-name", bus["native_id"])
            values = select(rules, "Rules")
            evidence["item_count"] = len(values)
            discovered = [self.add("events.rule", region, v["Arn"], dict(v, EventBusName=bus["native_id"]), evidence, v["Name"]) for v in values]
            for rule in self.limited(discovered, f"{region}:events:rules:{bus['native_id']}"):
                targets, target_record = self.call("events", "list-targets-by-rule", region,
                    "--event-bus-name", bus["native_id"], "--rule", rule["name"], query=EVENT_TARGET_QUERY)
                rule["observations"]["targets"] = dict(status=target_record["status"], data=targets,
                    evidence_id=target_record["id"], error_code=target_record["error_code"])
                rule["evidence_ids"].append(target_record["id"])

    # ---- account posture, AWS-computed recommendations, discovery -------------------------------------------
    def state(self, service, operation, kind, region, *arguments, query=None, native=None, attributes=None):
        """One call recorded as one account-level resource, whatever the outcome (ok, not_enabled, denied...)."""
        data, record = self.call(service, operation, region, *arguments, query=query)
        payload = attributes(data) if (attributes and data is not None) else (data if isinstance(data, dict) else {"items": data} if data is not None else {})
        resource = self.add(kind, region, native or self.args.expected_account, payload, record)
        with self.lock:
            resource["collection"] = {"status": record["status"], "error_code": record["error_code"]}
        return data, record, resource

    def securityhub(self, region):
        data, record, hub = self.state("securityhub", "describe-hub", "securityhub.hub", region)
        if data is not None:
            self.observe(hub, "standards", "securityhub", "get-enabled-standards", region)

    def guardduty(self, region):
        data, record, detectors = self.state("guardduty", "list-detectors", "guardduty.detector", region,
                                             attributes=lambda d: {"DetectorIds": d.get("DetectorIds", [])})
        for detector_id in (data or {}).get("DetectorIds", [])[:3]:
            self.observe(detectors, f"detector:{detector_id}", "guardduty", "get-detector", region, "--detector-id", detector_id)

    def config(self, region):
        data, record, recorders = self.state("config", "describe-configuration-recorders", "config.recorder", region)
        if data is None:
            return
        for label, operation in (("status", "describe-configuration-recorder-status"), ("delivery", "describe-delivery-channels"),
                                 ("resource_counts", "get-discovered-resource-counts")):
            self.observe(recorders, label, "config", operation, region)
        self.observe(recorders, "rules", "config", "describe-config-rules", region,
                     query="ConfigRules[].{Name:ConfigRuleName,Owner:Source.Owner,Identifier:Source.SourceIdentifier,State:ConfigRuleState}")
        counts = (recorders["observations"].get("resource_counts", {}).get("data") or {}).get("resourceCounts") or []
        if counts:
            with self.lock:
                self.discovery.setdefault(region, {}).setdefault("config_resource_counts", {})
                for item in counts:
                    self.discovery[region]["config_resource_counts"][item.get("resourceType")] = item.get("count")

    def inspector2(self, region):
        self.state("inspector2", "batch-get-account-status", "inspector2.account-status", region,
                   "--account-ids", self.args.expected_account)

    def accessanalyzer(self, region):
        data, record, analyzers = self.state("accessanalyzer", "list-analyzers", "accessanalyzer.analyzer", region)
        for analyzer in [a for a in (data or {}).get("analyzers", []) if a.get("status") == "ACTIVE"][:3]:
            self.observe(analyzers, f"findings:{analyzer.get('name')}", "accessanalyzer", "list-findings-v2", region,
                         "--analyzer-arn", analyzer["arn"], "--filter", '{"status":{"eq":["ACTIVE"]}}',
                         "--max-items", "200", query=ACCESS_FINDINGS_QUERY)

    def computeoptimizer(self, region):
        data, record, enrollment = self.state("computeoptimizer", "get-enrollment-status", "computeoptimizer.enrollment", region)
        if (data or {}).get("status") != "Active":
            return
        self.observe(enrollment, "summaries", "computeoptimizer", "get-recommendation-summaries", region)
        self.observe(enrollment, "ec2_recommendations", "computeoptimizer", "get-ec2-instance-recommendations", region,
                     "--max-results", "100", query=EC2_RECOMMENDATION_QUERY)
        self.observe(enrollment, "asg_recommendations", "computeoptimizer", "get-auto-scaling-group-recommendations", region,
                     "--max-results", "50")

    def support(self, region):
        """Trusted Advisor through the support API: Business, Enterprise On-Ramp or Enterprise support plan only."""
        data, record, advisor = self.state("support", "describe-trusted-advisor-checks", "support.trusted-advisor", region,
                                           "--language", "en",
                                           attributes=lambda d: {"checks": [{k: c.get(k) for k in ("id", "name", "category")} for c in d.get("checks", [])]})
        with self.lock:
            advisor["attributes"]["support_plan"] = ("business_or_higher" if record["status"] == "ok" else
                                                     "basic_or_developer" if record["status"] == "not_enabled" else "unknown")
        ids = [c["id"] for c in (data or {}).get("checks", []) if c.get("id")]
        for start in range(0, len(ids), 100):  # the summaries call accepts up to 100 check ids
            self.observe(advisor, f"summaries:{start // 100}", "support", "describe-trusted-advisor-check-summaries", region,
                         "--check-ids", *ids[start:start + 100])

    def ce(self, region):
        """Account bill for the last three closed months from Cost Explorer, grouped by service: real billing figures (unblended
        cost, USD) for the whole account, not an estimate and not attributed to the workload (cost allocation tags are a payer-account
        setting). The report shows the trend and the services that drive it with that caveat."""
        today = datetime.now(timezone.utc).date()
        first_of_month = today.replace(day=1)
        start = first_of_month
        for _ in range(3):
            start = (start - timedelta(days=1)).replace(day=1)

        def summarize(data):
            months = []
            for block in (data or {}).get("ResultsByTime") or []:
                groups = [(g.get("Keys", ["?"])[0], float(((g.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount") or 0)) for g in block.get("Groups") or []]
                groups.sort(key=lambda kv: -kv[1])
                months.append({"start": (block.get("TimePeriod") or {}).get("Start"), "end": (block.get("TimePeriod") or {}).get("End"),
                               "total_usd": round(sum(v for _, v in groups), 2), "services": [{"service": k, "usd": round(v, 2)} for k, v in groups[:12]],
                               "service_count": len(groups)})
            return {"metric": "UnblendedCost", "currency": "USD", "scope": "account", "months": months}
        self.state("ce", "get-cost-and-usage", "ce.cost-by-service", region, "--time-period", f"Start={start.isoformat()},End={first_of_month.isoformat()}",
                   "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE", attributes=summarize)

    def health(self, region):
        self.state("health", "describe-events", "health.events", region, "--filter", "eventStatusCodes=open,upcoming",
                   "--max-items", "200", query=HEALTH_QUERY, attributes=lambda d: {"events": d if isinstance(d, list) else d.get("events", [])})

    def organizations(self, region):
        data, record, org = self.state("organizations", "describe-organization", "organizations.organization", region)
        if data is not None:
            self.observe(org, "landing_zones", "controltower", "list-landing-zones", self.args.regions[0])

    def discover(self, region):
        """Tagged (or once tagged) resources of every service in the region, ARNs and tag keys only."""
        # --page-size keeps the CLI's automatic pagination (an API-level --resources-per-page would disable it).
        data, record = self.call("tagging", "get-resources", region, "--page-size", "100", query=TAGGING_QUERY)
        summary = {"services": {}, "resource_types": {}, "total": 0, "cloudformation_managed": 0, "status": record["status"],
                   "note": "Resource Groups Tagging API lists resources that are or were tagged; never-tagged resources are not counted."}
        for item in data or []:
            service, kind = parse_arn(item.get("ARN", ""))
            if not service:
                continue
            summary["total"] += 1
            summary["services"][service] = summary["services"].get(service, 0) + 1
            summary["resource_types"][f"{service}:{kind}"] = summary["resource_types"].get(f"{service}:{kind}", 0) + 1
            if any(k.startswith("aws:cloudformation:") for k in (item.get("Keys") or [])):
                summary["cloudformation_managed"] += 1
        self.add("discovery.tagged-resources", region, region, summary, record)
        with self.lock:
            self.discovery.setdefault(region, {})["tagging"] = summary
        return summary

    def auto_enable(self):
        """Add optional listings for services discovery found that the scope did not name."""
        found = set()
        for region_summary in self.discovery.values():
            for service in (region_summary.get("tagging") or {}).get("services", {}):
                target = ARN_SERVICE.get(service, service)
                found.add(target)
                if target == "bedrock":
                    found.add("bedrock-agent")
            for cfn_type in (region_summary.get("config_resource_counts") or {}):
                parts = cfn_type.split("::")
                if len(parts) == 3:
                    found.add(ARN_SERVICE.get(parts[1].lower(), parts[1].lower()))
        enable = sorted(x for x in found if x in SUPPORTED and x not in self.args.services and (x in OPTIONAL or x in PLATFORM_CONFIG or x in {"sqs", "sns", "events", "opensearch", "redshift"}))
        if enable:
            self.auto_enabled = enable
            self.set_services(self.args.services + enable)
        return enable

    def service_coverage(self):
        """Per service: how many resources discovery saw and whether this snapshot collected it."""
        seen = {}
        for region, summary in self.discovery.items():
            for service, count in (summary.get("tagging") or {}).get("services", {}).items():
                seen[service] = seen.get(service, 0) + count
        rows = {}
        for service in sorted(set(seen) | set(self.args.services)):
            collector = ARN_SERVICE.get(service, service)
            calls = [c for c in self.calls if c["service"] == collector or (collector == "cloudwatch" and c["service"] == "logs")]
            statuses = {c["status"] for c in calls}
            if collector in self.args.services or service in self.args.services:
                if calls and statuses <= {"not_enabled", "skipped"}:
                    status = "not_enabled" if "not_enabled" in statuses else "skipped"
                elif calls and "ok" not in statuses and statuses & {"access_denied"}:
                    status = "denied"
                else:
                    status = "collected"
            elif service in SUPPLEMENT_SERVICES or collector in SUPPLEMENT_SERVICES:
                status = "supplement_when_in_scope"
            else:
                status = "not_collected"
            rows[service] = {"discovered_resources": seen.get(service, 0), "collector": collector if collector in SUPPORTED else None,
                             "status": status, "auto_enabled": collector in self.auto_enabled}
        return rows

    def relationships(self):
        index = {(r["region"], r["type"], r["native_id"]): r for r in self.resources.values()}
        arn_index = {}
        for r in self.resources.values():
            for arn in [r["native_id"], *(r["attributes"].get(k) for k in ("Arn", "QueueArn", "TopicArn", "FunctionArn"))]:
                if isinstance(arn, str) and arn.startswith("arn:"):
                    arn_index[arn] = r
        edges = {}

        def link(source, kind, native, relation):
            target = index.get((source["region"], kind, str(native)))
            if target and source["id"] != target["id"]:
                key = (source["id"], target["id"], relation)
                edges[key] = {"source": source["id"], "target": target["id"], "relation": relation,
                              "evidence_ids": source["evidence_ids"], "certainty": "observed_configuration"}

        for resource in self.resources.values():
            a = resource["attributes"]
            if resource["type"] == "events.rule":
                link(resource, "events.bus", a.get("EventBusName"), "contained_in")
                for target in resource["observations"].get("targets", {}).get("data") or []:
                    observed = arn_index.get(target.get("Arn"))
                    if observed:
                        link(resource, observed["type"], observed["native_id"], "configured_target")
            if resource["type"] == "sqs.queue" and a.get("RedrivePolicy"):
                try:
                    target = arn_index.get(json.loads(a["RedrivePolicy"]).get("deadLetterTargetArn"))
                    if target:
                        link(resource, target["type"], target["native_id"], "configured_dead_letter")
                except (ValueError, TypeError, AttributeError):
                    pass  # Retain raw policy; do not invent a relationship.
            link(resource, "ec2.vpc", a.get("VpcId"), "contained_in")
            link(resource, "ec2.subnet", a.get("SubnetId"), "contained_in")
            for group in a.get("SecurityGroups", []):
                link(resource, "ec2.security-group", group.get("GroupId") if isinstance(group, dict) else group, "attached_to")
            for group in a.get("VpcSecurityGroups", []):
                link(resource, "ec2.security-group", group.get("VpcSecurityGroupId"), "attached_to")
            if resource["type"] == "ec2.volume":
                for attachment in a.get("Attachments", []):
                    link(resource, "ec2.instance", attachment.get("InstanceId"), "attached_to")
            if resource["type"] == "elbv2.target-group":
                for lb in a.get("LoadBalancerArns", []):
                    link(resource, "elbv2.load-balancer", lb, "associated_with")
                if a.get("TargetType") == "instance":
                    detail = resource["observations"].get("target_health", {}).get("data") or {}
                    for target in detail.get("TargetHealthDescriptions", []):
                        link(resource, "ec2.instance", target.get("Target", {}).get("Id"), "registered_target")
            if resource["type"] == "ecs.service":
                link(resource, "ecs.cluster", a.get("clusterArn"), "contained_in")
                for lb in a.get("loadBalancers") or []:
                    if isinstance(lb, dict) and lb.get("targetGroupArn"):
                        link(resource, "elbv2.target-group", lb["targetGroupArn"], "registered_target")
            if resource["type"] == "rds.instance":
                link(resource, "rds.cluster", a.get("DBClusterIdentifier"), "member_of")
        return list(edges.values())

    def run(self):
        self.root.mkdir(parents=True, exist_ok=False)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        identity, evidence = self.call("sts", "get-caller-identity", self.args.regions[0])
        if not identity or identity.get("Account") != self.args.expected_account:
            self.fatal = True
            self.finish("failed", "identity_failed_or_account_mismatch")
            return 1
        self.partition = identity.get("Arn", "arn:aws:").split(":")[1]
        if not getattr(self.args, "no_discovery", False):
            self.unit.name = None
            self.each(self.discover, self.args.regions)
            self.auto_enable()
        units = []
        for service in self.args.services:
            if service in GLOBAL_REGION:
                fixed = GLOBAL_REGION[service]
                regions = [fixed if fixed and self.partition == "aws" else self.args.regions[0]]
            else:
                regions = self.args.regions
            units += [(service, region) for region in regions]
        for service, _ in units:
            self.pending[service] = self.pending.get(service, 0) + 1
        self.each(self.run_unit, units)
        partial = self.truncations or any(c["status"] not in ("ok", "not_enabled") for c in self.calls)
        status = "failed" if self.fatal else ("partial" if partial else "complete")
        self.finish(status)
        return 1 if self.fatal else (2 if partial else 0)

    def run_unit(self, unit):
        try:
            self.collect_unit(*unit)
        finally:
            with self.lock:
                self.pending[unit[0]] -= 1
                if not self.pending[unit[0]]:
                    self.done.add(unit[0])

    def collect_unit(self, service, region):
        self.unit.name = service
        if self.fatal:
            return
        if service == "s3":
            self.s3(region)
        elif service == "iam":
            data, record = self.call("iam", "get-account-summary", region)
            self.add("iam.account-summary", "global", self.args.expected_account, data or {}, record)
        elif service in BASE:
            self.base(service, region)
        elif service in ("ecs", "eks"):
            self.clusters(service, region)
        elif service in PLATFORM_CONFIG:
            self.platform(service, region)
        elif service in OPTIONAL:
            self.optional(service, region)
        else:
            getattr(self, service.replace("-", "_"))(region)

    def finish(self, status, reason=None):
        coverage = {key: sum(c["status"] == key for c in self.calls)
                    for key in ("ok", "access_denied", "not_enabled", "expired_token", "not_found", "throttled", "error", "skipped")}
        common = {"schema_version": "1.0", "run_id": self.root.parent.name,
                  "account_id": self.args.expected_account, "regions": self.args.regions,
                  "services": self.args.services}
        manifest = dict(common, status=status, partition=self.partition, started_at=self.started,
                        completed_at=now(), read_only=True, auth_mode="profile" if self.args.profile else "environment",
                        budget_unit="aws_cli_invocations",
                        limits={"max_api_calls": self.args.max_api_calls, "max_detail_resources": self.args.max_detail_resources,
                                "max_workers": self.workers, "reserved_calls_per_service": self.share},
                        api_calls_by_service={k: v for k, v in self.used_by.items() if k is not None},
                        api_calls_executed=self.used, coverage=coverage, calls=self.calls,
                        truncations=self.truncations, failure_reason=reason,
                        discovery=self.discovery, auto_enabled_services=self.auto_enabled,
                        service_coverage=self.service_coverage() if status != "failed" else {})
        inventory = dict(common, collection_status=status, resources=list(self.resources.values()),
                         relationships=self.relationships(),
                         scope_note="Configuration metadata in explicitly selected regions/services; not traffic, code, SQL or utilization evidence.")
        write_json(self.root / "collection-manifest.json", manifest)
        write_json(self.root / "inventory.json", inventory)
        print(json.dumps({"status": status, "resources": len(self.resources), "api_calls": self.used,
                          "coverage": coverage, "auto_enabled": self.auto_enabled,
                          "discovered_services": sorted({s for d in self.discovery.values() for s in (d.get("tagging") or {}).get("services", {})}),
                          "output": str(self.root)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--profile")
    auth.add_argument("--environment", action="store_true")
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--regions", required=True, help="Comma-separated, explicitly scoped regions")
    parser.add_argument("--services", default=",".join(DEFAULT_SERVICES),
                        help="Default: " + ",".join(DEFAULT_SERVICES) + ". Also supported (enabled automatically when discovery "
                             "finds the service, or on request): " + ",".join(x for x in SUPPORTED if x not in DEFAULT_SERVICES))
    parser.add_argument("--no-discovery", action="store_true",
                        help="Skip the Resource Groups Tagging API scan that lists every tagged service and enables optional listings")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-api-calls", type=int, default=500,
                        help="Limit logical CLI invocations; pagination and retries can make more HTTP requests")
    parser.add_argument("--max-detail-resources", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--max-workers", type=int, default=8,
                        help="Concurrent AWS CLI invocations (1 = sequential). Half of --max-api-calls is reserved "
                             "in equal shares per service so one service cannot starve the rest")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    args.regions = list(dict.fromkeys(args.regions.split(",")))
    args.services = list(dict.fromkeys(args.services.split(",")))
    if not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be 12 digits")
    if any(not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", r) for r in args.regions):
        parser.error("invalid region")
    if any(s not in SUPPORTED for s in args.services):
        parser.error("unsupported service; consult --help")
    if min(args.max_api_calls, args.max_detail_resources, args.timeout, args.max_workers) < 1:
        parser.error("limits must be positive")
    if args.plan_only:
        print(json.dumps({"regions": args.regions, "services": args.services, "max_api_calls": args.max_api_calls,
                          "base_operations": {s: [x[0] for x in BASE.get(s, [])] for s in args.services},
                          "optional_operations": {s: [x[0] for x in OPTIONAL.get(s, [])] for s in args.services if s in OPTIONAL},
                          "discovery": [] if args.no_discovery else ["resourcegroupstaggingapi get-resources (ARNs and tag keys)"],
                          "detail_operations": sorted(" ".join(x) for x in DETAIL_OPERATIONS
                                                      if x[0] in args.services or x[0] in {"sts", "logs", "s3control", "tagging"})}, indent=2))
        return 0
    if args.output.exists():
        parser.error("output exists; use a fresh snapshot directory")
    problem = verify_skill_files() or assessment_gate(args.output)
    if problem:
        print(f"Collection stopped: {problem}", file=sys.stderr)
        return 1
    try:
        return Collector(args).run()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Collection stopped: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
