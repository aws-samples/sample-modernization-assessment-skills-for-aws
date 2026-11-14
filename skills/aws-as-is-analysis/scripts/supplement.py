#!/usr/bin/env python3
"""Read-only workload enrichment and scope resolution after collect.py. Python stdlib + AWS CLI v2.

Follows the scoped workload instead of the whole account:

1. Stacks named by scope.json (type=stack), stacks carrying the scope tags (type=tag), or every stack
   (type=account, capped): describe-stacks without Output/Parameter values, the original template,
   and the stack resource list.
2. Scope resolution: inventory resources whose identifiers match a stack physical ID or the scope tags,
   plus every inventory resource inside a VPC that is itself in scope (this picks up resources created
   outside CloudFormation, such as load balancers made by the AWS Load Balancer Controller or EKS nodes).
   Written to scope-resources.json with the reason for each resource.
3. Type-driven metadata for in-scope resources (listener rules and attributes, WAF association, task
   definitions and task placement, EKS node groups/add-ons/access entries, ElastiCache, secret rotation
   metadata, ECR settings, IAM role policy documents, pipelines and builds, alarm subscriptions,
   Bedrock invocation logging and guardrails, optional pinned source files).

Every operation is a describe/list/get metadata call checked against an allowlist. Secret values,
decrypted parameters, object contents, log events and database data are never requested. Values under
sensitive keys, task definition environment values and inline credential patterns are redacted before
anything is written; the record names what was removed. Output matches merge_supplementary_evidence.py.

  python3 supplement.py --as-is RUN/03-as-is --scope RUN/scope.json --profile P --output RUN/03-supplementary
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
READ_VERBS = ("describe-", "list-", "get-", "batch-get-")
DENY = {
    ("secretsmanager", "get-secret-value"), ("ssm", "get-parameter"), ("ssm", "get-parameters"),
    ("ssm", "get-parameters-by-path"), ("s3api", "get-object"), ("logs", "get-log-events"),
    ("logs", "get-query-results"), ("lambda", "get-function"), ("kms", "decrypt"),
    ("dynamodb", "get-item"), ("ecr", "get-login-password"), ("ecr", "get-authorization-token"),
    ("sts", "get-session-token"), ("codecommit", "get-blob"), ("eks", "get-token"),
}
SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|secret(?!sarn|arn|name|manager|id)|token|apikey|api_key|privatekey|credential|masteruserpassword)")
NON_SECRET_KEYS = {"HttpTokens", "imagePullCredentialsType", "HttpPutResponseHopLimit", "SecretsManagerSecretArn",
                   "KmsKeyId", "AuthTokenEnabled", "AuthTokenLastModifiedDate", "TokenValidity"}
SAFE_REF = re.compile(r"(?i)^(\{\{resolve:|arn:aws[a-z-]*:secretsmanager|arn:aws[a-z-]*:ssm|!Ref|\$\{)")
INLINE = [
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)([ \t]*[=:][ \t]*)(['\"]?)(?!\{\{resolve:|!|\$\{|\[REDACTED)[^\s'\"]{4,}"), r"\1\2\3[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED-AKID]"),
    (re.compile(r"(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\"\s]+"), r"\1[REDACTED]"),
]
AUTH_FATAL = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException",
              "SignatureDoesNotMatch"}
ACCOUNT_LEVEL = {"iam.account-summary", "s3.account-public-access-block"}


def load_collect():
    spec = importlib.util.spec_from_file_location("supplement_collect", HERE / "collect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now():
    return datetime.now(timezone.utc).isoformat()


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


def redact(value, path, removed):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = f"{path}.{key}"
            if (SENSITIVE_KEY.search(key) and key not in NON_SECRET_KEYS and isinstance(item, str)
                    and not SAFE_REF.match(item)):
                out[key] = "[REDACTED]"
                removed.append(here)
            elif key == "environment" and isinstance(item, list):
                out[key] = [{"name": e.get("name"), "value": "[REDACTED]"} if isinstance(e, dict) else e for e in item]
                removed.append(here + "[*].value")
            else:
                out[key] = redact(item, here, removed)
        return out
    if isinstance(value, list):
        return [redact(item, f"{path}[{i}]", removed) for i, item in enumerate(value)]
    if isinstance(value, str) and not value.startswith("arn:"):
        for pattern, replacement in INLINE:
            changed = pattern.sub(replacement, value)
            if changed != value:
                removed.append(path + " (inline pattern)")
                value = changed
    return value


def strip_stacks(data):
    for stack in data.get("Stacks", []):
        stack.pop("Outputs", None)
        stack["Parameters"] = [{"ParameterKey": p.get("ParameterKey")} for p in stack.get("Parameters", [])]
    return data


def decode_user_data(data):
    raw = (data.get("UserData") or {}).get("Value") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8", "replace")
    except ValueError:
        text = ""
    return {"InstanceId": data.get("InstanceId"), "UserData": {"Decoded": text}}


def decode_launch_templates(data):
    for version in data.get("LaunchTemplateVersions", []):
        user = (version.get("LaunchTemplateData") or {}).get("UserData")
        if user:
            try:
                version["LaunchTemplateData"]["UserData"] = {"Decoded": base64.b64decode(user).decode("utf-8", "replace")}
            except ValueError:
                version["LaunchTemplateData"]["UserData"] = {"Decoded": ""}
    return data


def decode_file(limit):
    def transform(data):
        raw = base64.b64decode(data.get("fileContent", "") or b"")
        return {"filePath": data.get("filePath"), "blobId": data.get("blobId"), "commitId": data.get("commitId"),
                "fileSize": data.get("fileSize"), "content": raw.decode("utf-8", "replace")[:limit]}
    return transform


def trim_builds(data):
    keys = ("id", "buildStatus", "startTime", "endTime", "sourceVersion", "resolvedSourceVersion", "currentPhase")
    return {"builds": [{k: b.get(k) for k in keys} for b in data.get("builds", [])]}


def redact_subscriptions(data):
    return {"Subscriptions": [{"Protocol": s.get("Protocol"), "SubscriptionArn": (s.get("SubscriptionArn") or "")[:120],
                               "Endpoint": "[REDACTED]"} for s in data.get("Subscriptions", [])]}


class Supplement:
    def __init__(self, args, inventory, manifest, scope):
        self.args, self.inventory, self.manifest, self.scope = args, inventory, manifest, scope
        collect = load_collect()
        self.env = collect.environment(args.profile)
        self.aws = shutil.which("aws")
        if not self.aws:
            raise ValueError("AWS CLI v2 missing")
        self.records, self.lock, self.used, self.fatal = [], threading.Lock(), 0, False
        self.region = inventory["regions"][0]
        self.by_native = {}
        for r in inventory["resources"]:
            for key in self.identifiers(r):
                self.by_native.setdefault(key, []).append(r["id"])

    @staticmethod
    def identifiers(resource):
        keys = {resource["native_id"], resource.get("name") or ""}
        a = resource["attributes"]
        for k in ("Arn", "LoadBalancerArn", "TargetGroupArn", "DBClusterIdentifier", "DBInstanceIdentifier", "GroupId",
                  "InstanceId", "VpcId" if resource["type"] == "ec2.vpc" else "", "SubnetId" if resource["type"] == "ec2.subnet" else "",
                  "TopicArn", "QueueUrl", "FunctionName", "logGroupName", "AlarmName", "clusterName", "serviceName", "name"):
            if k and isinstance(a.get(k), str):
                keys.add(a[k])
        if resource["type"] == "ecs.service" and isinstance(a.get("serviceArn"), str):
            keys.add(a["serviceArn"])
        return {k for k in keys if k}

    # ---- calls -------------------------------------------------------------------------------
    def plan(self, service, operation, *arguments, kind="configuration", origin="workload", locator="",
             resource_ids=(), complete=True, transform=None, empty_ok=False):
        if not operation.startswith(READ_VERBS) or (service, operation) in DENY:
            raise ValueError(f"operation not allowed: {service} {operation}")
        with self.lock:
            rid = f"s{len(self.records) + 1:04d}"
            record = {"id": rid, "service": service, "operation": operation, "region": self.region,
                      "arguments": [str(a) for a in arguments], "collection_method": "aws_cli",
                      "collected_at": None, "evidence_kind": kind, "response_complete": complete,
                      "resource_ids": sorted(set(resource_ids)),
                      "provenance": {"origin": origin, "locator": locator, "redactions": []}}
            self.records.append(record)
        return record, transform, empty_ok

    def execute(self, planned):
        record, transform, empty_ok = planned
        record["collected_at"] = now()
        with self.lock:
            over = self.used >= self.args.max_calls
            if not over:
                self.used += 1
        if self.fatal or over:
            record.update(status="skipped", error_code="auth_failed" if self.fatal else "supplement_budget_exhausted",
                          raw_path=None, sha256=None, raw_format="json")
            return None
        argv = [record["service"], record["operation"], *record["arguments"], "--region", self.region,
                "--output", "json", "--no-cli-pager", "--profile", self.args.profile]
        try:
            response = subprocess.run(["aws", *checked_arguments(argv)], capture_output=True, text=True, env=self.env,  # nosec B603 B607 - see checked_arguments
                                      timeout=self.args.timeout)
        except subprocess.TimeoutExpired:
            record.update(status="error", error_code="timeout", return_code=-1, raw_path=None, sha256=None,
                          raw_format="json", validation_error="timeout")
            return None
        record["return_code"] = response.returncode
        if response.returncode:
            match = re.search(r"\(([A-Za-z0-9_.-]+)\)", response.stderr)
            code = match.group(1) if match else "cli_or_network_error"
            status = "error"
            if code in AUTH_FATAL:
                status, self.fatal = "expired_token", True
            elif "AccessDenied" in code or "Unauthorized" in code or "not authorized" in response.stderr:
                status = "access_denied"
            elif "Throttl" in code or code in {"RequestLimitExceeded", "TooManyRequestsException"}:
                status = "throttled"
            elif any(w in code for w in ("NotFound", "NoSuch", "NotExist")) or "does not exist" in response.stderr:
                status = "not_found"
            record.update(status=status, error_code=code, raw_path=None, sha256=None, raw_format="json")
            return None
        text = response.stdout.strip()
        if not text:
            if not empty_ok:
                record.update(status="error", error_code="blank_stdout", validation_error="blank stdout",
                              raw_path=None, sha256=None, raw_format="json")
                return None
            data = {}
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                record.update(status="error", error_code="parse_error", validation_error="invalid JSON",
                              raw_path=None, sha256=None, raw_format="json")
                return None
        if transform:
            data = transform(data)
        removed = []
        data = redact(data, "$", removed)
        body = json.dumps(data, indent=1, ensure_ascii=False, default=str).encode()
        name = f"{record['id']}-{record['service']}-{record['operation']}.json"
        (self.args.output / name).write_bytes(body)
        record.update(status="ok", error_code=None, raw_path=name, sha256=hashlib.sha256(body).hexdigest(), raw_format="json")
        record["provenance"]["redactions"] = sorted(set(removed))[:200]
        return data

    def run(self, planned):
        """Execute planned calls concurrently; return payloads in plan order."""
        if not planned:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(self.args.max_workers, len(planned)))) as pool:
            return list(pool.map(self.execute, planned))

    # ---- discovery ---------------------------------------------------------------------------
    def inv_ids(self, *natives):
        found = set()
        for n in natives:
            if not n:
                continue
            for key, ids in self.by_native.items():
                if key == n or (len(n) > 8 and (key.endswith("/" + n) or key.endswith(":" + n))):
                    found.update(ids)
        return found

    def stacks(self):
        wl = self.scope["workload"]
        excluded = set(wl.get("exclude_stacks") or [])
        if wl["type"] == "stack":
            return [s for s in wl["stack_names"] if s not in excluded]
        listing = self.run([self.plan("cloudformation", "describe-stacks", locator="stack names and tags only",
                                      transform=lambda d: {"Stacks": [{"StackName": s.get("StackName"), "Tags": s.get("Tags", []),
                                                                        "StackStatus": s.get("StackStatus")} for s in d.get("Stacks", [])]})])[0] or {}
        names = []
        for s in listing.get("Stacks", []):
            if s["StackName"] in excluded or str(s.get("StackStatus", "")).startswith("DELETE"):
                continue
            tags = {t.get("Key"): t.get("Value") for t in s.get("Tags", [])}
            if wl["type"] == "account" or all(tags.get(k) == v for k, v in (wl.get("tags") or {}).items()):
                names.append(s["StackName"])
        return names[: self.args.max_stacks]

    def collect(self):
        names = self.stacks()
        plans = []
        for name in names:
            plans += [self.plan("cloudformation", "describe-stacks", "--stack-name", name, origin=name,
                                locator="Stacks[0] (Outputs and Parameter values dropped)", transform=strip_stacks),
                      self.plan("cloudformation", "get-template", "--stack-name", name, "--template-stage", "Original",
                                kind="template_declared", origin=name, locator="TemplateBody"),
                      self.plan("cloudformation", "list-stack-resources", "--stack-name", name, origin=name,
                                locator="StackResourceSummaries")]
        results = self.run(plans)
        stack_resources = []
        templates = {}
        for i, name in enumerate(names):
            templates[name] = (results[3 * i + 1] or {}).get("TemplateBody")
            for r in (results[3 * i + 2] or {}).get("StackResourceSummaries", []):
                stack_resources.append(dict(r, StackName=name))
        scope = self.resolve_scope(names, stack_resources)
        self.enrich(stack_resources, scope, templates)
        return names, scope

    def resolve_scope(self, names, stack_resources):
        reasons = {}

        def add(rid, why):
            reasons.setdefault(rid, set()).add(why)
        for r in stack_resources:
            for rid in self.inv_ids(r.get("PhysicalResourceId")):
                add(rid, f"stack:{r['StackName']}/{r['LogicalResourceId']}")
        tags = self.scope["workload"].get("tags") or {}
        if tags:
            for res in self.inventory["resources"]:
                a = res["attributes"]
                found = {t.get("Key") or t.get("key"): t.get("Value") or t.get("value") for t in (a.get("Tags") or a.get("tags") or []) if isinstance(t, dict)}
                if isinstance(a.get("tags"), dict):
                    found.update(a["tags"])
                if all(found.get(k) == v for k, v in tags.items()):
                    add(res["id"], "tag")
        if self.scope["workload"]["type"] == "account":
            for res in self.inventory["resources"]:
                add(res["id"], "account")
        by_id = {r["id"]: r for r in self.inventory["resources"]}
        vpcs = {by_id[rid]["native_id"] for rid in reasons if by_id[rid]["type"] == "ec2.vpc"}
        subnet_vpc = {r["native_id"]: r["attributes"].get("VpcId") for r in self.inventory["resources"] if r["type"] == "ec2.subnet"}
        for res in self.inventory["resources"]:
            vpc = self.vpc_of(res, subnet_vpc)
            if vpc in vpcs and res["id"] not in reasons:
                add(res["id"], f"in-scope-vpc:{vpc}")
        scoped_instances = {by_id[rid]["native_id"] for rid in reasons if by_id[rid]["type"] == "ec2.instance"}
        for res in self.inventory["resources"]:
            if res["type"] == "ec2.volume" and any(x.get("InstanceId") in scoped_instances for x in res["attributes"].get("Attachments", [])):
                add(res["id"], "attached-to-scoped-instance")
            # The SSM record of a scoped instance (OS name and version) belongs to that instance, not to a VPC.
            if res["type"] == "ssm.managed-instance" and res["native_id"] in scoped_instances:
                add(res["id"], "managed-instance-of-scoped-instance")
        # Context: the VPCs and subnets of scoped resources whose VPC is not itself in scope are kept as
        # placement context so the diagram can show where those resources sit.
        for rid in list(reasons):
            vpc = self.vpc_of(by_id[rid], subnet_vpc)
            for res in self.inventory["resources"]:
                if vpc and vpc not in vpcs and ((res["type"] == "ec2.vpc" and res["native_id"] == vpc) or
                                                 (res["type"] == "ec2.subnet" and res["attributes"].get("VpcId") == vpc)):
                    add(res["id"], f"placement-context:{vpc}")
        for res in self.inventory["resources"]:
            # Account posture, discovery summaries and account-wide settings are not workload resources; the review
            # reads them through their `level` marker, so they stay out of the workload scope here.
            if res["type"] in ACCOUNT_LEVEL or (res.get("level") == "account" and res["type"] != "cloudtrail.trail"):
                reasons.pop(res["id"], None)
            elif res["type"] == "cloudtrail.trail":
                add(res["id"], "account-level-governance")
        return {"resources": [{"id": rid, "reasons": sorted(why)} for rid, why in sorted(reasons.items())],
                "vpcs": sorted(vpcs), "stack_resources": [
                    {k: r.get(k) for k in ("StackName", "LogicalResourceId", "PhysicalResourceId", "ResourceType")}
                    for r in stack_resources]}

    @staticmethod
    def vpc_of(res, subnet_vpc):
        a = res["attributes"]
        if res["type"] == "ec2.vpc":
            return res["native_id"]
        def field(name, key):
            value = a.get(name)
            return value.get(key) if isinstance(value, dict) else None
        vpc = a.get("VpcId") or field("resourcesVpcConfig", "vpcId") or field("DBSubnetGroup", "VpcId")
        if vpc:
            return vpc
        if a.get("SubnetId"):
            return subnet_vpc.get(a["SubnetId"])
        subnets = (((a.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}).get("subnets") or [])
        return subnet_vpc.get(subnets[0]) if subnets else None

    def enrich(self, stack_resources, scope, templates):
        scoped = {x["id"] for x in scope["resources"]}
        res = [r for r in self.inventory["resources"] if r["id"] in scoped]
        typed = lambda kind: [r for r in res if r["type"] == kind]  # noqa: E731
        by_type = {}
        for r in stack_resources:
            by_type.setdefault(r.get("ResourceType"), []).append(r)
        phys = lambda t: sorted({r["PhysicalResourceId"] for r in by_type.get(t, []) if r.get("PhysicalResourceId")})  # noqa: E731
        plans = []

        # Load balancers: every scoped LB, including ones created outside CloudFormation.
        for lb in typed("elbv2.load-balancer"):
            arn, ids = lb["native_id"], [lb["id"]]
            plans.append(self.plan("elbv2", "describe-load-balancer-attributes", "--load-balancer-arn", arn, resource_ids=ids))
            if lb["attributes"].get("Type") == "application":
                plans.append(self.plan("wafv2", "get-web-acl-for-resource", "--resource-arn", arn, resource_ids=ids, empty_ok=True))
            for listener in (lb["observations"].get("listeners", {}).get("data") or {}).get("Listeners", []):
                plans.append(self.plan("elbv2", "describe-rules", "--listener-arn", listener["ListenerArn"], resource_ids=ids))
        for tg in typed("elbv2.target-group"):
            plans.append(self.plan("elbv2", "describe-target-group-attributes", "--target-group-arn", tg["native_id"], resource_ids=[tg["id"]]))
        # ECS: task definitions, running task placement.
        clusters = {}
        for svc in typed("ecs.service"):
            a = svc["attributes"]
            if a.get("taskDefinition"):
                plans.append(self.plan("ecs", "describe-task-definition", "--task-definition", a["taskDefinition"],
                                       locator="taskDefinition (environment values redacted)", resource_ids=[svc["id"]]))
            if a.get("clusterArn"):
                clusters.setdefault(a["clusterArn"], []).append(svc["id"])
        if typed("ecs.service"):
            plans.append(self.plan("application-autoscaling", "describe-scalable-targets", "--service-namespace", "ecs",
                                   resource_ids=[x["id"] for x in typed("ecs.service")]))
        for cluster, ids in clusters.items():
            plans.append(self.plan("ecs", "list-tasks", "--cluster", cluster, "--desired-status", "RUNNING", resource_ids=ids))
        # EKS
        for eks in typed("eks.cluster"):
            name, ids = eks["attributes"].get("name") or eks["native_id"], [eks["id"]]
            for op in ("list-nodegroups", "list-addons", "list-access-entries", "list-pod-identity-associations"):
                plans.append(self.plan("eks", op, "--cluster-name", name, resource_ids=ids))
        # EC2 instances: bootstrap and management state (no command execution).
        instances = typed("ec2.instance")
        for inst in instances[: self.args.max_instances]:
            plans.append(self.plan("ec2", "describe-instance-attribute", "--instance-id", inst["native_id"], "--attribute", "userData",
                                   kind="template_declared", locator="UserData decoded, inline secrets redacted",
                                   resource_ids=[inst["id"]], transform=decode_user_data))
            profile = (inst["attributes"].get("IamInstanceProfile") or {}).get("Arn")
            if profile:
                plans.append(self.plan("iam", "get-instance-profile", "--instance-profile-name", profile.rsplit("/", 1)[-1], resource_ids=[inst["id"]]))
        if instances:
            plans.append(self.plan("ssm", "describe-instance-information", "--filters",
                                   "Key=InstanceIds,Values=" + ",".join(i["native_id"] for i in instances[:50]),
                                   resource_ids=[i["id"] for i in instances[:50]]))
        for lt in phys("AWS::EC2::LaunchTemplate"):
            plans.append(self.plan("ec2", "describe-launch-template-versions", "--launch-template-id", lt, "--versions", "$Latest",
                                   kind="template_declared", locator="UserData decoded, inline secrets redacted", transform=decode_launch_templates))
        # Engine lifecycle: standard-support end dates for every RDS/Aurora engine major version and for the
        # Kubernetes versions in use. Judged in review against today's date (RDS_ENGINE_VERSION_SUPPORT, EKS_VERSION_SUPPORT).
        engines = {}
        for db in typed("rds.instance") + typed("rds.cluster"):
            engine, version = db["attributes"].get("Engine"), str(db["attributes"].get("EngineVersion") or "")
            if engine and version:
                major = version.split(".")[0] if not str(engine).startswith("aurora") or version[:1].isdigit() else version.split(".mysql_aurora.")[0]
                if str(engine) in ("mysql", "aurora-mysql", "mariadb") and version.count(".") >= 1:
                    major = ".".join(version.split(".")[:2])
                engines.setdefault((engine, major), []).append(db["id"])
        for (engine, major), ids in sorted(engines.items()):
            plans.append(self.plan("rds", "describe-db-major-engine-versions", "--engine", engine, "--major-engine-version", major,
                                   locator="lifecycle support dates for this major version", resource_ids=ids, empty_ok=True))
        if typed("eks.cluster"):
            plans.append(self.plan("eks", "describe-cluster-versions", locator="Kubernetes version support dates",
                                   resource_ids=[c["id"] for c in typed("eks.cluster")]))
        # Data tier
        for cluster in typed("rds.cluster"):
            plans.append(self.plan("rds", "describe-db-cluster-snapshots", "--db-cluster-identifier", cluster["native_id"],
                                   "--max-items", "20", complete=False, locator="latest 20 snapshots", resource_ids=[cluster["id"]]))
        for group in phys("AWS::ElastiCache::ReplicationGroup"):
            plans.append(self.plan("elasticache", "describe-replication-groups", "--replication-group-id", group))
        for cache in phys("AWS::ElastiCache::CacheCluster"):
            plans.append(self.plan("elasticache", "describe-cache-clusters", "--cache-cluster-id", cache, "--show-cache-node-info"))
        for secret in phys("AWS::SecretsManager::Secret"):
            plans.append(self.plan("secretsmanager", "describe-secret", "--secret-id", secret, locator="metadata only; no GetSecretValue"))
        if typed("rds.cluster") or typed("rds.instance") or typed("dynamodb.table") or by_type.get("AWS::EFS::FileSystem"):
            plans += [self.plan("backup", "list-backup-plans"), self.plan("backup", "list-protected-resources")]
        # Containers registry
        repos = [p.rsplit("/", 1)[-1] if p.startswith("arn:") else p for p in phys("AWS::ECR::Repository")]
        if repos:
            plans.append(self.plan("ecr", "describe-repositories", "--repository-names", *repos))
            for repo in repos:
                plans.append(self.plan("ecr", "describe-images", "--repository-name", repo, "--max-items", "20", complete=False,
                                       locator="up to 20 images"))
        # Delivery
        for pipeline in phys("AWS::CodePipeline::Pipeline"):
            plans += [self.plan("codepipeline", "get-pipeline", "--name", pipeline),
                      self.plan("codepipeline", "get-pipeline-state", "--name", pipeline),
                      self.plan("codepipeline", "list-pipeline-executions", "--pipeline-name", pipeline, "--max-items", "20",
                                complete=False, locator="latest 20 executions")]
        projects = phys("AWS::CodeBuild::Project")
        if projects:
            plans.append(self.plan("codebuild", "batch-get-projects", "--names", *projects))
        if self.args.source_files and phys("AWS::CodeCommit::Repository"):
            plans.append(self.plan("codecommit", "list-repositories", locator="names and IDs only"))
        # Observability: log groups in scope plus the newest stream of each (is anything still being written?),
        # CloudWatch agent metrics for EC2 hosts, and the alarms that cover scoped resources (judged in review).
        log_groups = sorted(set(phys("AWS::Logs::LogGroup")) | {g["native_id"] for g in typed("logs.group")})
        for group in log_groups:
            plans.append(self.plan("logs", "describe-log-groups", "--log-group-name-prefix", group))
            plans.append(self.plan("logs", "describe-log-streams", "--log-group-name", group, "--order-by", "LastEventTime",
                                   "--descending", "--max-items", "1", complete=False, locator="newest stream: name and lastEventTimestamp only",
                                   resource_ids=self.inv_ids(group) & scoped,
                                   transform=lambda d: {"logStreams": [{k: x.get(k) for k in ("logStreamName", "lastEventTimestamp", "lastIngestionTime")}
                                                                       for x in d.get("logStreams", [])[:1]]}))
        if instances:
            plans.append(self.plan("cloudwatch", "list-metrics", "--namespace", "CWAgent", "--max-items", "50", complete=False,
                                   locator="CloudWatch agent metric names and dimensions (no values)",
                                   resource_ids=[i["id"] for i in instances[:50]],
                                   transform=lambda d: {"Metrics": [{"MetricName": m.get("MetricName"),
                                                                     "InstanceIds": sorted({x.get("Value") for x in m.get("Dimensions", []) if x.get("Name") == "InstanceId"})}
                                                                    for m in d.get("Metrics", [])]}))
        for topic in sorted(set(phys("AWS::SNS::Topic")) | {t["native_id"] for t in typed("sns.topic")}):
            plans.append(self.plan("sns", "list-subscriptions-by-topic", "--topic-arn", topic, transform=redact_subscriptions,
                                   resource_ids=self.inv_ids(topic) & scoped))
        for dash in phys("AWS::CloudWatch::Dashboard"):
            plans.append(self.plan("cloudwatch", "get-dashboard", "--dashboard-name", dash))
        for rule in phys("AWS::Events::Rule"):
            plans.append(self.plan("events", "describe-rule", "--name", rule.split("|")[-1]))
        # GenAI governance: only when the workload declares Bedrock use.
        text = json.dumps(templates, default=str) + json.dumps([r["attributes"] for r in res], default=str)
        if re.search(r"bedrock", text, re.I):
            plans += [self.plan("bedrock", "get-model-invocation-logging-configuration", empty_ok=True,
                                locator="account/region invocation logging"),
                      self.plan("bedrock", "list-guardrails", locator="guardrails defined in region")]
        self.scoped, self.roles_seen = scoped, set()
        self.repo_ids = set(phys("AWS::CodeCommit::Repository"))
        for arn in phys("AWS::IAM::Role"):
            plans += self.role_plans(arn if arn.startswith("arn:") else f"role/{arn}", set())
        pending = plans
        while pending:  # follow discoveries wave by wave until nothing new is planned
            results = self.run(pending)
            pending = [x for planned, data in zip(pending, results) if data for x in self.follow(planned[0], data)]

    def role_plans(self, arn, ids):
        name = arn.rsplit("/", 1)[-1]
        if name in self.roles_seen:
            return []
        self.roles_seen.add(name)
        ids = set(ids) & self.scoped
        return [self.plan("iam", op, "--role-name", name, origin=arn, resource_ids=ids)
                for op in ("get-role", "list-attached-role-policies", "list-role-policies")]

    def follow(self, record, data):
        """Plan the next metadata calls a successful response makes possible."""
        op, ids, args = (record["service"], record["operation"]), record["resource_ids"], record["arguments"]
        out = []
        if op == ("ecs", "describe-task-definition"):
            td = data.get("taskDefinition", {})
            for key in ("taskRoleArn", "executionRoleArn"):
                if td.get(key):
                    out += self.role_plans(td[key], ids)
        elif op == ("ecs", "list-tasks") and data.get("taskArns"):
            out.append(self.plan("ecs", "describe-tasks", "--cluster", args[1], "--tasks", *data["taskArns"][:20],
                                 locator="task placement (AZ, subnet)", resource_ids=ids,
                                 transform=lambda d: {"tasks": [{k: t.get(k) for k in ("taskArn", "group", "availabilityZone", "lastStatus",
                                                                                       "taskDefinitionArn", "attachments", "healthStatus")}
                                                                for t in d.get("tasks", [])]}))
        elif op == ("eks", "list-nodegroups"):
            out += [self.plan("eks", "describe-nodegroup", "--cluster-name", args[1], "--nodegroup-name", ng, resource_ids=ids)
                    for ng in data.get("nodegroups", [])]
        elif op == ("eks", "describe-nodegroup") and (data.get("nodegroup") or {}).get("nodeRole"):
            out += self.role_plans(data["nodegroup"]["nodeRole"], ids)
        elif op == ("eks", "list-addons"):
            out += [self.plan("eks", "describe-addon", "--cluster-name", args[1], "--addon-name", a, resource_ids=ids)
                    for a in data.get("addons", [])]
        elif op == ("eks", "list-pod-identity-associations"):
            out += [self.plan("eks", "describe-pod-identity-association", "--cluster-name", args[1], "--association-id",
                              a["associationId"], resource_ids=ids) for a in data.get("associations", [])]
        elif op == ("eks", "describe-pod-identity-association") and (data.get("association") or {}).get("roleArn"):
            out += self.role_plans(data["association"]["roleArn"], ids)
        elif op == ("iam", "get-instance-profile"):
            for role in (data.get("InstanceProfile") or {}).get("Roles", []):
                out += self.role_plans(role["Arn"], ids)
        elif op == ("iam", "list-role-policies"):
            out += [self.plan("iam", "get-role-policy", "--role-name", args[1], "--policy-name", name, origin=record["provenance"]["origin"],
                              locator="inline policy document", resource_ids=ids) for name in data.get("PolicyNames", [])]
        elif op == ("iam", "list-attached-role-policies"):
            # AWS managed policies are public; only customer managed documents are fetched.
            out += [self.plan("iam", "get-policy", "--policy-arn", p["PolicyArn"], origin=record["provenance"]["origin"], resource_ids=ids)
                    for p in data.get("AttachedPolicies", []) if ":aws:policy/" not in p["PolicyArn"]]
        elif op == ("iam", "get-policy"):
            policy = data.get("Policy") or {}
            out.append(self.plan("iam", "get-policy-version", "--policy-arn", policy.get("Arn", ""), "--version-id",
                                 policy.get("DefaultVersionId", "v1"), origin=record["provenance"]["origin"],
                                 locator="default policy version document", resource_ids=ids))
        elif op == ("codebuild", "batch-get-projects"):
            for project in data.get("projects", []):
                if project.get("serviceRole"):
                    out += self.role_plans(project["serviceRole"], set())
                out.append(self.plan("codebuild", "list-builds-for-project", "--project-name", project["name"], "--max-items", "10",
                                     complete=False, locator="latest 10 builds"))
        elif op == ("codebuild", "list-builds-for-project") and data.get("ids"):
            out.append(self.plan("codebuild", "batch-get-builds", "--ids", *data["ids"][:10], complete=False, transform=trim_builds))
        elif op == ("codepipeline", "get-pipeline") and (data.get("pipeline") or {}).get("roleArn"):
            out += self.role_plans(data["pipeline"]["roleArn"], set())
        elif op == ("codecommit", "list-repositories"):
            for repo in data.get("repositories", []):
                if repo.get("repositoryId") in self.repo_ids or repo.get("repositoryName") in self.repo_ids:
                    out.append(self.plan("codecommit", "get-repository", "--repository-name", repo["repositoryName"], origin=repo["repositoryName"]))
        elif op == ("codecommit", "get-repository"):
            meta = data.get("repositoryMetadata") or {}
            out.append(self.plan("codecommit", "get-branch", "--repository-name", meta.get("repositoryName", ""), "--branch-name",
                                 meta.get("defaultBranch") or "main", origin=meta.get("repositoryName", "")))
        elif op == ("codecommit", "get-branch"):
            commit = (data.get("branch") or {}).get("commitId")
            if commit:
                out.append(self.plan("codecommit", "get-folder", "--repository-name", args[1], "--commit-specifier", commit,
                                     "--folder-path", "/", kind="source_declared", origin=args[1], locator=f"@{commit}:/"))
                out[-1][0]["provenance"]["revision"] = commit
        elif op == ("codecommit", "get-folder"):
            repo, commit = args[1], record["provenance"]["revision"]
            files = [f["absolutePath"] for f in data.get("files", [])]
            key = lambda p: (not re.search(r"(Dockerfile|buildspec|requirements|package\.json|\.ya?ml|app\.|main\.|server\.)", p), p)  # noqa: E731
            for path in sorted(files, key=key)[: self.args.source_files]:
                out.append(self.plan("codecommit", "get-file", "--repository-name", repo, "--commit-specifier", commit, "--file-path", path,
                                     kind="source_declared", origin=repo, locator=f"@{commit}:{path}", transform=decode_file(20000)))
                out[-1][0]["provenance"]["revision"] = commit
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-is", type=Path, required=True, help="03-as-is snapshot from collect.py")
    parser.add_argument("--scope", type=Path, required=True, help="scope.json from aws-cli-setup")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--max-stacks", type=int, default=20)
    parser.add_argument("--max-instances", type=int, default=30)
    parser.add_argument("--source-files", type=int, default=25, help="pinned CodeCommit files to read for in-scope build projects (0 = none)")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    shared = load_collect()
    problem = shared.verify_skill_files() or shared.assessment_gate(args.output)
    if problem:
        print(f"Supplement stopped: {problem}", file=sys.stderr)
        return 1
    try:
        inventory = json.loads((args.as_is / "inventory.json").read_text(encoding="utf-8"))
        manifest = json.loads((args.as_is / "collection-manifest.json").read_text(encoding="utf-8"))
        scope = json.loads(args.scope.read_text(encoding="utf-8"))
        if scope.get("account_id") != inventory["account_id"]:
            raise ValueError("scope.json account differs from the snapshot")
        if args.output.exists():
            raise ValueError("output exists; use a fresh directory")
        args.output.mkdir(parents=True)
        job = Supplement(args, inventory, manifest, scope)
        stacks, resolved = job.collect()
        records = [r for r in job.records if r.get("status")]
        if not records:
            raise ValueError("no supplementary records")
        meta = {"schema_version": "1.0", "account_id": inventory["account_id"], "regions": [job.region],
                "stacks": stacks, "max_calls": args.max_calls, "calls_executed": job.used, "records": records}
        (args.output / "supplementary-manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        scope_doc = {"schema_version": "1.0", "account_id": inventory["account_id"], "run_id": inventory["run_id"],
                     "workload": scope.get("workload_name"), "scope_type": scope["workload"]["type"], "stacks": stacks, **resolved}
        (args.output / "scope-resources.json").write_text(json.dumps(scope_doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        status = {}
        for r in records:
            status[r["status"]] = status.get(r["status"], 0) + 1
        print(json.dumps({"status": "collected", "records": len(records), "by_status": status, "stacks": len(stacks),
                          "in_scope_resources": len(resolved["resources"]), "output": str(args.output)}))
        return 0 if not job.fatal else 1
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"Supplement stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
