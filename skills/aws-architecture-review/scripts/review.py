#!/usr/bin/env python3
"""Evidence-checked starter assessment; local scores are not official AWS WA scores."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_kit  # noqa: E402  (shared visual report kit, standard library only)
import iac_fixes  # noqa: E402
import platform_rules  # noqa: E402
import workload_rules  # noqa: E402

PILLARS = ("Operational Excellence", "Security", "Reliability",
           "Performance Efficiency", "Cost Optimization", "Sustainability")
STATUSES = {"PASS", "FAIL", "WARN", "UNKNOWN", "NOT_APPLICABLE"}
SH = "https://docs.aws.amazon.com/securityhub/latest/userguide/"
WA = "https://docs.aws.amazon.com/wellarchitected/latest/"
RULESET_VERSION = "moda-starter-1.3"
# Pillars whose HIGH baseline severity becomes MEDIUM when the run scope states a non-production environment. Security
# keeps its severity: non-production environments often hold copies of real data.
NON_PRODUCTION_PILLARS = ("Reliability", "Operational Excellence", "Performance Efficiency", "Cost Optimization", "Sustainability")
NON_PRODUCTION_BASIS = "HIGH in production; MEDIUM because the scope states a non-production environment"


CONFIDENCE = {"configuration", "template_declared", "source_declared", "runtime_observed",
              "measured", "documented", "inferred", "unverified"}


def validate_payload(call, raw):
    """An integrity hash alone must not turn missing output into evidence."""
    if not raw.strip():
        raise ValueError("successful evidence has blank payload")
    kind = call.get("raw_format")
    if kind is None:  # Existing collector/host snapshots use JSON; legacy sources may use text.
        kind = "json" if str(call.get("raw_path", "")).endswith(".json") else "text"
    if kind == "json":
        value = json.loads(raw)
        if not isinstance(value, (dict, list)):
            raise ValueError("successful JSON evidence must be an object or array")
    elif kind != "text":
        raise ValueError("unsupported evidence payload format")


def nested(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def flag(value, false_status="FAIL"):
    return "UNKNOWN" if value is None else ("PASS" if value else false_status)


def public_admin_permission(permissions):
    if not isinstance(permissions, list):
        return None
    for rule in permissions:
        public = any(x.get("CidrIp") == "0.0.0.0/0" for x in rule.get("IpRanges", [])) or any(
            x.get("CidrIpv6") == "::/0" for x in rule.get("Ipv6Ranges", []))
        if not public:
            continue
        protocol = str(rule.get("IpProtocol", ""))
        if protocol == "-1":
            return True
        if protocol in {"tcp", "6"}:
            low, high = rule.get("FromPort"), rule.get("ToPort")
            if isinstance(low, int) and isinstance(high, int) and any(low <= p <= high for p in (22, 3389)):
                return True
    return False


def verify_inputs(root):
    inventory = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "collection-manifest.json").read_text(encoding="utf-8"))
    if inventory.get("schema_version") != "1.0" or manifest.get("schema_version") != "1.0":
        raise ValueError("unsupported input schema")
    if manifest["status"] == "failed" or inventory["account_id"] != manifest["account_id"]:
        raise ValueError("failed or mismatched collection")
    for key in ("run_id", "regions", "services"):
        if inventory.get(key) != manifest.get(key):
            raise ValueError(f"snapshot {key} mismatch")
    calls = {c["id"]: c for c in manifest["calls"]}
    if len(calls) != len(manifest["calls"]):
        raise ValueError("duplicate evidence IDs")
    for call in calls.values():
        if call.get("raw_path"):
            path = (root / call["raw_path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("evidence path escapes snapshot")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != call["sha256"]:
                raise ValueError(f"evidence checksum mismatch: {call['id']}")
            if call["status"] == "ok":
                validate_payload(call, raw)
        elif call["status"] == "ok":
            raise ValueError("successful evidence has no raw file")
    resource_ids = {r["id"] for r in inventory["resources"]}
    if len(resource_ids) != len(inventory["resources"]):
        raise ValueError("duplicate resource IDs")
    for resource in inventory["resources"]:
        if resource["account_id"] != inventory["account_id"]:
            raise ValueError("resource account mismatch")
        if any(e not in calls for e in resource["evidence_ids"]):
            raise ValueError("resource references unknown evidence")
    for edge in inventory["relationships"]:
        if edge["source"] not in resource_ids or edge["target"] not in resource_ids:
            raise ValueError("dangling relationship")
        if any(e not in calls for e in edge["evidence_ids"]):
            raise ValueError("relationship references unknown evidence")
    return inventory, manifest, calls


def load_scope(path, inventory):
    """In-scope resource IDs from supplement.py (scope-resources.json); None means the whole snapshot."""
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("account_id") != inventory["account_id"]:
        raise ValueError("scope-resources.json belongs to another account")
    known = {r["id"] for r in inventory["resources"]}
    ids = {x["id"] for x in data.get("resources", [])}
    if ids - known:
        raise ValueError("scope-resources.json names resources missing from the snapshot")
    return ids


def load_run_scope(path, inventory):
    """The run's scope.json (aws-cli-setup): workload name and context.environment; None when there is no file."""
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("account_id") not in (None, inventory["account_id"]):
        raise ValueError("scope.json belongs to another account")
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    environment = context.get("environment") if context.get("environment") in ("production", "non-production") else "unknown"
    return {"workload_name": data.get("workload_name"), "environment": environment}


def names(items, limit=5):
    """'a, b and c' with at most `limit` names; the rest are counted."""
    items = list(dict.fromkeys(str(x) for x in items if x))
    if len(items) > limit:
        items = items[:limit] + [f"{len(items) - limit} more"]
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def owner_questions(resources, run_scope):
    """The five owner questions, naming the in-scope resources and, for a non-production scope, the environment."""
    workload = (run_scope or {}).get("workload_name") or "this workload"
    nonprod = (run_scope or {}).get("environment") == "non-production"
    env = f"the non-production {workload} environment" if (run_scope or {}).get("workload_name") else "the non-production environment"

    def of(*kinds):
        return [r for r in resources if r["type"] in kinds and r.get("level") != "account"]
    instances = [r for r in of("ec2.instance") if not workload_rules.is_eks_node(r["attributes"])]
    services = ([f"ECS service {r['name']}" for r in of("ecs.service")] + [f"EKS cluster {r['name']}" for r in of("eks.cluster")]
                + [f"EC2 instance {workload_rules.instance_label(r)}" for r in instances if not workload_rules.is_admin_host(r["attributes"])]
                + [f"Lambda function {r['name']}" for r in of("lambda.function")])
    alarms = [r["name"] for r in of("cloudwatch.alarm")]
    engines = {"aurora": "Aurora cluster", "neptune": "Neptune cluster", "docdb": "DocumentDB cluster"}
    databases = ([f"{next((v for k, v in engines.items() if str(r['attributes'].get('Engine', '')).startswith(k)), 'DB cluster')} {r['name']}" for r in of("rds.cluster")]
                 + [f"RDS instance {r['name']}" for r in of("rds.instance") if not r["attributes"].get("DBClusterIdentifier")]
                 + [f"DynamoDB table {r['name']}" for r in of("dynamodb.table")] + [f"ElastiCache replication group {r['name']}" for r in of("elasticache.replication-group")])
    entries = ([f"load balancer {r['name']}" for r in of("elbv2.load-balancer")] + [f"API {r['name']}" for r in of("apigateway.rest-api", "apigatewayv2.api")]
               + [f"distribution {r['name']}" for r in of("cloudfront.distribution")])
    compute = ([f"EKS cluster {r['name']} nodes" for r in of("eks.cluster")] + [f"ECS service {r['name']}" for r in of("ecs.service")]
               + [f"EC2 instance {r['native_id']}" for r in instances] + [f"database {r['name']}" for r in of("rds.instance")])
    ops = (f"Who owns {workload} in operation, is there a deployment runbook for " + (names(services) if services else "each component")
           + ", and is there a runbook for each alarm" + (f" ({names(alarms)})" if alarms else "") + "?")
    restore = ((f"What recovery time and data-loss limits (RTO/RPO) apply to {names(databases)}" if databases else "What recovery time and data-loss limits (RTO/RPO) apply")
               + (f" in {env}" if nonprod else "") + ", and when were restore and failover last tested?")
    perf = ((f"Which response time and request volume must {env} support (for example in load tests before a release)" if nonprod else
             "Which latency and availability targets (SLOs) apply") + (f" at {names(entries)}" if entries else "")
            + ", and can you share two to four weeks of load and latency metrics?")
    cost = (f"Is there a monthly budget for {env}, and do Savings Plans or Reserved Instances cover any of it?" if nonprod else
            f"Can you share the monthly cost of {workload} and any Savings Plans or Reserved Instances that cover it?")
    sus = ((f"Does {env} run outside working hours, or could " + (names(compute, 6) if compute else "its compute") + " be stopped or scaled down at night "
            "and at weekends? How long must each kind of data be kept?") if nonprod else
           ("How busy are " + (names(compute, 6) if compute else "the compute resources") + " compared with demand, and how long must each kind of data be kept?"))
    return {"OPS_PROCEDURES": ops, "REL_RESTORE_TEST": restore, "PERF_MEASUREMENT": perf, "COST_WORKLOAD": cost, "SUS_UTILIZATION": sus}


def apply_environment(checks, run_scope):
    """Non-production scope: HIGH baseline severities outside Security become MEDIUM and say why."""
    if (run_scope or {}).get("environment") != "non-production":
        return
    for check in checks:
        if check.get("severity") == "HIGH" and check.get("pillar") in NON_PRODUCTION_PILLARS:
            check["severity"], check["severity_basis"] = "MEDIUM", NON_PRODUCTION_BASIS


def evaluate(inventory, manifest, calls, root=None, scoped=None, run_scope=None):
    checks = []
    governance = {"cloudtrail.trail", "iam.account-summary", "s3.account-public-access-block"}
    resources = [r for r in inventory["resources"] if scoped is None or r["id"] in scoped or r["type"] in governance
                 or r.get("level") == "account"]
    kept = {r["id"] for r in resources}
    outside = {(r["type"], r["native_id"]) for r in inventory["resources"] if r["id"] not in kept}
    seen = set()

    def emit(rule, pillar, resource, status, observation, recommendation, url, weight=2, severity="MEDIUM", evidence=None, target=None, **extra):
        ids = evidence if evidence is not None else (resource["evidence_ids"] if resource else [])
        key = (rule, resource["id"] if resource else None, target)
        if key in seen:
            return
        seen.add(key)
        check = {
            "id": f"f{len(checks)+1:04d}", "rule_id": rule, "pillar": pillar,
            "resource_id": resource["id"] if resource else None, "status": status,
            "severity": severity, "weight": weight, "evidence_ids": list(dict.fromkeys(ids)),
            "observation": observation, "recommendation": recommendation, "source_urls": [url],
            "confidence": "configuration" if status not in {"UNKNOWN", "NOT_APPLICABLE"} else "unverified",
        }
        if target:
            check["target"] = target
        check.update({k: v for k, v in extra.items() if k in {"question", "related_pillars", "enablement", "severity_basis"} and v})
        checks.append(check)

    def typed(kind):
        return [r for r in resources if r["type"] == kind]

    definitions = (
        ("EC2_IMDSV2", "ec2.instance", "Security", "MetadataOptions.HttpTokens", lambda a: a.get("MetadataOptions", {}).get("HttpTokens") == "required" if a.get("MetadataOptions", {}).get("HttpTokens") is not None else None,
         "Verify that IMDSv2 is required and that the application is compatible.", SH+"ec2-controls.html#ec2-8", "FAIL"),
        ("EBS_ENCRYPTION", "ec2.volume", "Security", "Encrypted", lambda a: a.get("Encrypted"),
         "Review EBS encryption at rest and KMS key policies.", SH+"ec2-controls.html#ec2-3", "FAIL"),
        ("RDS_ENCRYPTION", "rds.instance", "Security", "StorageEncrypted", lambda a: a.get("StorageEncrypted"),
         "Plan engine-specific encryption migration and key operations.", SH+"rds-controls.html#rds-3", "FAIL"),
        ("RDS_PUBLIC_CONFIG", "rds.instance", "Security", "PubliclyAccessible", lambda a: None if "PubliclyAccessible" not in a else not a["PubliclyAccessible"],
         "Review public access configuration, security groups, routing and required access paths together.", SH+"rds-controls.html#rds-2", "FAIL"),
        ("RDS_BACKUPS", "rds.instance", "Reliability", "BackupRetentionPeriod", lambda a: None if "BackupRetentionPeriod" not in a else a["BackupRetentionPeriod"] > 0,
         "Verify automated backups against the required RPO and inspect restore-test evidence.", SH+"rds-controls.html#rds-11", "FAIL"),
    )
    for rule, kind, pillar, field, predicate, recommendation, url, false_status in definitions:
        for resource in typed(kind):
            value = predicate(resource["attributes"])
            emit(rule, pillar, resource, flag(value, false_status), f"{field} configuration check", recommendation, url)
    for resource in typed("ec2.security-group"):
        exposed = public_admin_permission(resource["attributes"].get("IpPermissions"))
        emit("SG_PUBLIC_ADMIN", "Security", resource, flag(None if exposed is None else not exposed),
             "Check public IPv4/IPv6 CIDRs and TCP ports 22/3389 or all protocols within the same ingress permission. Actual reachability requires separate evidence.",
             "Verify administrative access paths and allow only the required sources.", SH+"ec2-controls.html#ec2-19", weight=3, severity="HIGH")
    for resource in typed("rds.instance"):
        a = resource["attributes"]
        if str(a.get("Engine", "")).startswith("aurora"):
            status = "NOT_APPLICABLE"
            observation = "Aurora instance; availability is judged at cluster level (AURORA_SINGLE_INSTANCE)."
        else:
            status = flag(a.get("MultiAZ"), "WARN")
            observation = f"MultiAZ={a.get('MultiAZ', 'unknown')}; Verify workload criticality and failure tolerance."
        emit("RDS_RESILIENCE", "Reliability", resource, status, observation,
             "Compare the actual database placement with recovery requirements.", SH+"rds-controls.html#rds-5")
    for resource in typed("ec2.volume"):
        a = resource["attributes"]
        status = "UNKNOWN" if "State" not in a else ("WARN" if a["State"] == "available" and not a.get("Attachments") else "PASS")
        emit("EBS_UNATTACHED", "Cost Optimization", resource, status, f"EBS state={a.get('State', 'unknown')}",
             "Verify ownership, recovery and retention purposes, and utilization before optimizing. Do not delete automatically.",
             WA+"cost-optimization-pillar/cost_manage_demand_resources_decommission_resources.html", weight=1, severity="LOW")
    account_blocks = typed("s3.account-public-access-block")
    account_bpa = account_blocks[0] if account_blocks else None
    bpa_fields = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")

    def block_settings(data):
        config = (data or {}).get("PublicAccessBlockConfiguration")
        return config if isinstance(config, dict) else {}

    for resource in typed("s3.bucket"):
        observation = resource["observations"].get("public_access_block", {})
        account_settings = block_settings(account_bpa["attributes"]) if account_bpa else {}
        bucket_settings = block_settings(observation.get("data"))
        if observation.get("error_code") == "NoSuchPublicAccessBlockConfiguration":
            bucket_settings = dict.fromkeys(bpa_fields, False)
        if account_bpa:
            account_call = calls[account_bpa["evidence_ids"][0]]
            if account_call.get("error_code") == "NoSuchPublicAccessBlockConfiguration":
                account_settings = dict.fromkeys(bpa_fields, False)
        # S3 uses the most restrictive setting at each level, independently per flag.
        effective_flags = []
        for field in bpa_fields:
            pair = (account_settings.get(field), bucket_settings.get(field))
            effective_flags.append(True if any(v is True for v in pair) else
                                   (False if all(v is False for v in pair) else None))
        effective = True if all(v is True for v in effective_flags) else (
            False if any(v is False for v in effective_flags) else None)
        evidence = resource["evidence_ids"] + (account_bpa["evidence_ids"] if account_bpa else [])
        emit("S3_PUBLIC_ACCESS_BLOCK", "Security", resource, flag(effective),
             "Combine the most restrictive account and bucket settings for each of the four BPA flags. Actual public policies and ACLs require separate assessment.",
             "Review account and bucket BPA against public-access requirements and policies.", SH+"s3-controls.html#s3-8", evidence=evidence)
    for resource in typed("s3.bucket"):
        observation = resource["observations"].get("versioning", {})
        if observation.get("status") == "ok":
            state = (observation.get("data") or {}).get("Status") or "never enabled"
            emit("S3_VERSIONING", "Reliability", resource, "PASS" if state == "Enabled" else "WARN",
                 f"Versioning={state}; overwritten or deleted objects {'can' if state == 'Enabled' else 'cannot'} be recovered.",
                 "Enable versioning and add a lifecycle rule that expires noncurrent versions.",
                 "https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html", severity="LOW")
    for resource in typed("dynamodb.table"):
        observation = resource["observations"].get("continuous_backups", {})
        status = nested(observation.get("data"), "ContinuousBackupsDescription", "PointInTimeRecoveryDescription", "PointInTimeRecoveryStatus")
        emit("DYNAMODB_PITR", "Reliability", resource, flag(None if status is None else status == "ENABLED"),
             f"PITR={status or 'unknown'}", "Review PITR and restore procedures against the required RPO.", SH+"dynamodb-controls.html#dynamodb-2")
    for resource in typed("cloudtrail.trail"):
        logging = resource["observations"].get("logging", {})
        value = (logging.get("data") or {}).get("IsLogging")
        emit("CLOUDTRAIL_LOGGING", "Operational Excellence", resource, flag(value),
             f"IsLogging={value}; also review event selector scope and log delivery errors.",
             "Verify required event coverage, logging status and delivery errors.", SH+"cloudtrail-controls.html#cloudtrail-1")
    for resource in typed("logs.group"):
        days = resource["attributes"].get("retentionInDays")
        emit("LOG_RETENTION", "Operational Excellence", resource, "PASS" if days else "WARN",
             f"retentionInDays={days if days else 'never expire'}; the appropriate retention period depends on workload policy.",
             "Define retention periods based on operational and audit requirements and cost.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html", severity="LOW")
    # Service scope checks stop an empty or denied inventory from looking healthy.
    for service, kinds, pillar in (
        ("ec2", ("ec2.instance", "ec2.security-group", "ec2.volume"), "Security"),
        ("rds", ("rds.instance",), "Reliability"),
        ("s3", ("s3.bucket",), "Security"),
        ("dynamodb", ("dynamodb.table",), "Reliability"),
        ("cloudtrail", ("cloudtrail.trail",), "Operational Excellence"),
    ):
        if not any(r["type"] in kinds for r in resources):
            evidence = [c for c in manifest["calls"] if c["service"] == service]
            related = [t for t in manifest["truncations"] if service in str(t.get("scope", "")) or
                       any(c["id"] == t.get("scope") for c in evidence)]
            complete = service in inventory["services"] and evidence and all(c["status"] == "ok" for c in evidence) and not related
            # Cite the listing calls that prove absence in scope, not the per-resource detail calls of resources outside the
            # scope: the evidence index would otherwise name other projects' tables and trails in the customer's report.
            listings = [c for c in evidence if not [a for a in (c.get("arguments") or []) if not str(a).startswith("--")]] or evidence[:1]
            emit(f"SCOPE_{service.upper()}", pillar, None, "NOT_APPLICABLE" if complete else "UNKNOWN",
                 f"{service}: no matching resources in scope" if complete else f"{service}: not collected or insufficient evidence to establish absence",
                 "Collect missing evidence within the authorized scope when needed.", WA+"framework/welcome.html", evidence=[c["id"] for c in listings])
    # SEC_WAF: internet-facing application load balancers should sit behind a Web ACL. The judgement
    # uses the supplementary `wafv2 get-web-acl-for-resource` record for that ALB when one exists:
    # a documented empty answer (empty_response_meaning=no_web_acl_associated) is a judged absence.
    waf_calls = [c for c in manifest["calls"] if c.get("service") in {"wafv2", "waf-regional"}
                 and c.get("operation") == "get-web-acl-for-resource"]
    for resource in typed("elbv2.load-balancer"):
        a = resource["attributes"]
        if a.get("Type") != "application" or a.get("Scheme") != "internet-facing":
            continue
        arn = a.get("LoadBalancerArn") or ""
        matching = [c for c in waf_calls if arn and arn in " ".join(str(x) for x in c.get("arguments", []))]
        if not matching:
            emit("SEC_WAF", "Security", resource, "UNKNOWN",
                 "Web ACL association not collected for this internet-facing ALB",
                 "Collect `wafv2 get-web-acl-for-resource --resource-arn <alb-arn>` as supplementary evidence, or confirm WAF coverage with the owner.",
                 "https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-associating-aws-resource.html")
            continue
        call = matching[-1]
        evidence = resource["evidence_ids"] + [call["id"]]
        if call.get("status") == "ok" and call.get("empty_response_meaning") == "no_web_acl_associated":
            emit("SEC_WAF", "Security", resource, "WARN", "No AWS WAF Web ACL is associated with this internet-facing ALB",
                 "Associate a Web ACL with managed rule groups (Core rule set, known bad inputs, SQLi) and tune in count mode first.",
                 "https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-associating-aws-resource.html", evidence=evidence)
        elif call.get("status") == "ok":
            emit("SEC_WAF", "Security", resource, "PASS", "An AWS WAF Web ACL is associated with this ALB",
                 "Review the rule groups and logging of the associated Web ACL.",
                 "https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-associating-aws-resource.html", evidence=evidence)
        else:
            emit("SEC_WAF", "Security", resource, "UNKNOWN",
                 f"Web ACL association query failed ({call.get('error_code') or call.get('status')})",
                 "Grant wafv2:GetWebACLForResource or confirm WAF coverage with the owner.",
                 "https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-associating-aws-resource.html", evidence=evidence)
    records = workload_rules.Evidence(root if root is not None else Path("."), calls)
    if root is not None:
        workload_rules.evaluate_workload(emit, resources, records, kept, outside=outside if scoped is not None else set(),
                                         context={"environment": (run_scope or {}).get("environment"), "inventory": inventory,
                                                  "truncations": manifest.get("truncations") or []})
    platform_rules.evaluate_platform(emit, resources, records, kept)
    questions = owner_questions([r for r in resources if r["id"] in kept], run_scope)
    for rule, pillar, url in (
        ("OPS_PROCEDURES", "Operational Excellence", WA+"operational-excellence-pillar/welcome.html"),
        ("REL_RESTORE_TEST", "Reliability", WA+"reliability-pillar/welcome.html"),
        ("PERF_MEASUREMENT", "Performance Efficiency", WA+"performance-efficiency-pillar/welcome.html"),
        ("COST_WORKLOAD", "Cost Optimization", WA+"cost-optimization-pillar/welcome.html"),
        ("SUS_UTILIZATION", "Sustainability", WA+"sustainability-pillar/welcome.html"),
    ):
        emit(rule, pillar, None, "UNKNOWN", "Not visible in configuration metadata.", questions[rule], url, question=questions[rule])
    apply_environment(checks, run_scope)
    if root is not None:
        iac_fixes.attach(checks, inventory["resources"], records, lambda check: validate_iac_changes([check], root, calls), template_resource_block)
    return checks


def score(checks, calls, resources):
    ids = set()
    keys = set()
    for check in checks:
        if check["id"] in ids or check["status"] not in STATUSES or check["pillar"] not in PILLARS:
            raise ValueError("invalid or duplicate check")
        ids.add(check["id"])
        if "rule_id" in check:
            key = (check["rule_id"], check.get("resource_id"), check.get("target"))
            if key in keys:
                raise ValueError("duplicate rule/resource check")
            keys.add(key)
        if check["status"] in {"FAIL", "WARN"} and not (check.get("resource_id") or (isinstance(check.get("target"), str) and check["target"].strip())):
            raise ValueError(f"{check['id']}: a FAIL or WARN must name resource_id or target")
        for extra in check.get("related_pillars", []) or []:
            if extra not in PILLARS or extra == check["pillar"]:
                raise ValueError(f"{check['id']}: related_pillars must name other pillars")
        confidence = check.get("confidence")
        if confidence is not None and confidence not in CONFIDENCE:
            raise ValueError("unsupported finding confidence")
        if confidence in {"inferred", "unverified"} and check["status"] in {"PASS", "FAIL", "WARN"}:
            raise ValueError("unverified or inferred finding cannot have a judged score")
        if isinstance(check["weight"], bool) or not isinstance(check["weight"], int) or not 1 <= check["weight"] <= 5:
            raise ValueError("weight must be 1..5")
        if check.get("resource_id") and check["resource_id"] not in resources:
            raise ValueError("finding references unknown resource")
        if any(e not in calls for e in check["evidence_ids"]):
            raise ValueError("finding references unknown evidence")
        if check["status"] in {"PASS", "FAIL", "WARN"} and not any(calls[e]["status"] in ("ok", "not_enabled") for e in check["evidence_ids"]):
            raise ValueError("evaluated finding requires successful evidence")
    result = {}
    values = {"PASS": 1, "WARN": 0.5, "FAIL": 0}
    for pillar in PILLARS:
        items = [c for c in checks if c["pillar"] == pillar]
        judged = [c for c in items if c["status"] in values]
        applicable = [c for c in items if c["status"] != "NOT_APPLICABLE"]
        denominator = sum(c["weight"] for c in judged)
        result[pillar] = {
            "score": round(100 * sum(c["weight"] * values[c["status"]] for c in judged) / denominator, 1) if denominator else None,
            "evidence_coverage_percent": round(100 * len(judged) / len(applicable), 1) if applicable else None,
            "counts": dict(Counter(c["status"] for c in items)),
        }
    return result


def assessment_basis(inventory, baseline, checks):
    """Disclose assessment changes independently of the numeric score formula."""
    def key(c):
        return c["rule_id"], c.get("resource_id"), c.get("target")

    def identity(c):
        out = {"rule_id": c["rule_id"], "resource_id": c.get("resource_id")}
        if c.get("target"):
            out["target"] = c["target"]
        return out

    def fingerprint(items):
        shape = [dict(identity(c), pillar=c["pillar"], weight=c["weight"]) for c in items]
        shape.sort(key=lambda c: (c["rule_id"], c["resource_id"] or ""))
        data = {"ruleset_version": RULESET_VERSION, "account_id": inventory["account_id"],
                "regions": sorted(inventory["regions"]), "services": sorted(inventory["services"]),
                "checks": shape}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    before, after = {key(c): c for c in baseline}, {key(c): c for c in checks}
    modified = []
    for k in sorted(before.keys() & after.keys(), key=lambda k: (k[0], k[1] or "", k[2] or "")):
        old, new = before[k], after[k]
        changes = {field: {"before": old.get(field), "after": new.get(field)}
                   for field in ("pillar", "weight", "status", "severity")
                   if old.get(field) != new.get(field)}
        if changes:
            modified.append(dict(identity(new), changes=changes,
                                 assessment_reason=new.get("assessment_reason")))
    return {
        "schema_version": "1.0", "ruleset_version": RULESET_VERSION,
        "baseline_fingerprint": fingerprint(baseline), "assessment_fingerprint": fingerprint(checks),
        "baseline_check_count": len(baseline), "assessment_check_count": len(checks),
        "added": [identity(c) for c in checks if key(c) not in before],
        "removed": [identity(c) for c in baseline if key(c) not in after],
        "modified": modified,
        "same_check_scope_and_weights": fingerprint(baseline) == fingerprint(checks),
        "unexplained_modifications": [identity(c) for c in modified if not c.get("assessment_reason")],
    }


def manual_language_errors(manual):
    """Manual checks are written in English, like everything else in the canonical findings (translated at the end)."""
    items = []
    for m in manual:
        for field in ("observation", "recommendation", "question", "assessment_reason"):
            if m.get(field):
                items.append((f"{m.get('id', '?')}.{field}", m[field]))
    return report_kit.english_errors(items)


def merge_manual(baseline, manual):
    """Baseline checks plus agent-authored checks. A manual check with the same rule and target replaces the
    baseline one (a changed status, severity, pillar or weight needs assessment_reason); `{"id": ..., "suppress":
    true, "assessment_reason": ...}` drops a baseline check, for example one about a resource outside the workload."""
    def key(c):
        return c["rule_id"], c.get("resource_id"), c.get("target")
    by_id = {c["id"]: c for c in baseline}
    out = {key(c): c for c in baseline}
    for m in manual:
        if m.get("suppress"):
            if m.get("id") not in by_id or not m.get("assessment_reason"):
                raise ValueError("suppress needs a baseline check id and assessment_reason")
            out.pop(key(by_id[m["id"]]), None)
            continue
        base = out.get(key(m))
        if base and not m.get("assessment_reason") and any(base.get(f) != m.get(f) for f in ("status", "severity", "pillar", "weight")):
            raise ValueError(f"{m['id']} changes baseline {base['id']} without assessment_reason")
        out[key(m)] = m
    merged = list(out.values())
    if len({c["id"] for c in merged}) != len(merged):
        raise ValueError("manual check IDs collide with baseline IDs; use m0001-style IDs")
    return merged


def template_resource_block(body, logical_id):
    """Text of one resource in a CloudFormation template body (JSON object or YAML text), or None."""
    if isinstance(body, dict):
        resource = (body.get("Resources") or {}).get(logical_id)
        return None if resource is None else json.dumps(resource, sort_keys=True)
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.rstrip() == "Resources:")
    except StopIteration:
        return None
    pattern = re.compile(rf"^(\s+){re.escape(logical_id)}:\s*(#.*)?$")
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            return None  # next top-level section
        match = pattern.match(lines[i])
        if match:
            indent = len(match.group(1))
            block = [lines[i]]
            for line in lines[i + 1:]:
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                block.append(line)
            return "\n".join(block)
    return None


def validate_iac_changes(checks, root, calls):
    """Each proposed IaC change must point at a collected template and match what that template declares."""
    for check in checks:
        for change in check.get("iac_changes", []) or []:
            for field in ("stack", "logical_id", "property", "proposed", "evidence_id"):
                if not isinstance(change.get(field), str) or not change[field].strip():
                    raise ValueError(f"{check['id']}: iac change needs {field}")
            call = calls.get(change["evidence_id"])
            if not call or call.get("operation") != "get-template" or call.get("status") != "ok":
                raise ValueError(f"{check['id']}: iac change evidence must be a successful get-template record")
            if change["stack"] not in call.get("arguments", []):
                raise ValueError(f"{check['id']}: evidence {call['id']} is the template of another stack")
            body = json.loads((root / call["raw_path"]).read_text(encoding="utf-8")).get("TemplateBody")
            block = template_resource_block(body, change["logical_id"])
            if block is None:
                raise ValueError(f"{check['id']}: {change['logical_id']} is not a resource in {change['stack']}")
            prop = change["property"].split(".")[-1]
            current = change.get("current")
            if current is None:
                if re.search(rf"[\s\"']{re.escape(prop)}[\"']?\s*:", block):
                    raise ValueError(f"{check['id']}: {prop} is set in {change['logical_id']}; give its current value")
            elif str(current) not in block:
                raise ValueError(f"{check['id']}: current value {current!r} not found in {change['logical_id']}")
            if change["evidence_id"] not in check["evidence_ids"]:
                check["evidence_ids"].append(change["evidence_id"])


def read_svg(path):
    """The diagram text as rendered, after report_kit.scan_svg refused DTDs, scripts, handlers and external references."""
    text = path.read_text(encoding="utf-8")
    report_kit.scan_svg(text)
    return text


def render_report(output, inventory, manifest, checks, scores, diagram=None, basis=None, run_meta=None):
    status = "Assessment draft - final AWS diagram pending" if not diagram else "Assessment results - review the visual QA record"
    lines = ["# AWS architecture review", "", f"- Status: {status}",
             f"- Account: `{inventory['account_id']}` / Regions: {', '.join(inventory['regions'])}",
             f"- Collection status: `{manifest['status']}` / Collected at: {manifest['completed_at']}",
             "- Scores use local starter rules and are not official AWS Well-Architected scores or HRI/MRI ratings.", "",
             "| Pillar | Local score | Evidence coverage | Check counts |", "|---|---:|---:|---|"]
    for pillar, value in scores.items():
        lines.append(f"| {pillar} | {value['score'] if value['score'] is not None else 'Unassessed'} | {value['evidence_coverage_percent'] if value['evidence_coverage_percent'] is not None else 'Not applicable'} | {value['counts']} |")
    basis_note = ""
    if basis:
        basis_note = (
            f"Automatic baseline: {basis['baseline_check_count']} checks; current assessment: "
            f"{basis['assessment_check_count']}. Added {len(basis['added'])}, removed {len(basis['removed'])}, "
            f"modified {len(basis['modified'])} baseline checks. "
            "See assessment-basis.json and scorecard.baseline.json. Compare scores only with aligned scope, rules and weights.")
        lines += ["", "## Assessment basis", "", basis_note]
        if basis["unexplained_modifications"]:
            basis_note += " Some baseline changes lack assessment_reason; explain them before final delivery."
            lines.append("Some baseline changes lack assessment_reason; explain them before final delivery.")
    lines += ["", "## Architecture diagram", ""]
    diagram_markup = "<p>Final AWS diagram required</p>"
    if diagram:
        diagram_markup = read_svg(diagram)
        relative = os_relative(diagram, output)
        lines += [f"![AS-IS AWS architecture]({relative})"]
    else:
        lines += ["Draw the current-state diagram with diagram.py before delivery."]
    lines += ["", "## Findings requiring attention or evidence", ""]
    detailed = [c for c in checks if c["status"] not in {"PASS", "NOT_APPLICABLE"}]
    for pillar in PILLARS:
        items = [c for c in detailed if c["pillar"] == pillar]
        if items:
            lines += [f"### {pillar}", ""]
        for check in items:
            lines += [f"#### {check['id']} · {check['rule_id']} · {check['status']}",
                      f"- Pillar/severity: {check['pillar']} / {check['severity']}",
                      f"- Target: `{check['resource_id'] or 'workload/scope'}`",
                      f"- Confidence: {check.get('confidence', 'unverified')}",
                      f"- Observation: {check['observation']}", f"- Recommendation: {check['recommendation']}",
                      f"- Evidence: {', '.join(check['evidence_ids']) or 'Additional evidence required'}",
                      f"- Sources: {', '.join(check['source_urls'])}", ""]
    summarized = Counter((c["rule_id"], c["status"]) for c in checks if c not in detailed)
    summary_rows = [f"| {rule} | {state} | {count} |" for (rule, state), count in sorted(summarized.items())]
    lines += ["## Passed and not applicable checks", "", "| Rule | Status | Count |",
              "|---|---|---:|"] + summary_rows + ["", "Full check details remain in findings.json.", ""]
    lines += report_kit.run_metadata_markdown(run_meta)
    lines += ["## Scope and limitations", "", f"Collection scope: {', '.join(inventory['services'])}. Automated starter checks and unresolved manual checks are shown together.",
              f"Query results: `{json.dumps(manifest['coverage'], ensure_ascii=False)}`.",
              f"Detail collection limits: `{json.dumps(manifest['truncations'], ensure_ascii=False)}`.",
              "Actual traffic, code/database compatibility, SLOs, successful recovery, cost and business criticality require separate evidence.", ""]
    (output / "review.md").write_text("\n".join(lines), encoding="utf-8")
    document = render_html(inventory, manifest, checks, scores, status, diagram_markup, diagram, basis_note, summarized, run_meta)
    (output / "review.html").write_text(document, encoding="utf-8")


def render_html(inventory, manifest, checks, scores, status, diagram_markup, diagram, basis_note, summarized, run_meta=None):
    """Visual HTML report: KPI tiles, pillar/judgement/severity charts, inventory profile and grouped findings.

    Every number is read from findings/scorecard/inventory/manifest; charts are inline SVG without scripts.
    """
    kit = report_kit
    counts = Counter(c["status"] for c in checks)
    status_order = {"FAIL": 0, "WARN": 1, "UNKNOWN": 2}
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    type_counts = Counter(r["type"] for r in inventory["resources"]).most_common()
    inventory_pairs = [(kind, count) for kind, count in type_counts[:7]]
    if len(type_counts) > 7:
        inventory_pairs.append((f"other ({len(type_counts) - 7} types)", sum(c for _, c in type_counts[7:])))

    def finding_card(c):
        label, color, _ = kit.STATUS[c["status"]]
        severity_label, severity_color = kit.SEVERITY.get(c["severity"], ("Info", kit.SOFT))
        target = (c["resource_id"] or "").split(":", 3)[-1] if c["resource_id"] else "workload/scope"
        sources = " · ".join(f'<a href="{kit.esc(u)}" target="_blank" rel="noopener">{kit.esc(u.split("/")[2] if "//" in u else u)}</a>'
                             for u in c["source_urls"])
        return (f'<article class="finding" style="border-left-color:{color}"><header><strong>{kit.esc(c["id"])} · {kit.esc(c["rule_id"])}</strong>'
                f'{kit.status_chip(c["status"])}{kit.chip("severity " + severity_label, severity_color)}{kit.chip(c.get("confidence", "unverified"), kit.MUTED)}</header>'
                f'<p class="small">Target <code>{kit.esc(target)}</code></p><p><b>Observation</b> {kit.esc(c["observation"])}</p>'
                f'<p><b>Recommendation</b> {kit.esc(c["recommendation"])}</p>'
                f'<div class="ev">Evidence {kit.esc(", ".join(c["evidence_ids"]) or "additional evidence required")} · Sources {sources or "-"}</div></article>')

    pillar_sections = []
    for pillar in PILLARS:
        items = sorted([c for c in checks if c["pillar"] == pillar and c["status"] in status_order],
                       key=lambda c: (status_order[c["status"]], severity_order.get(c["severity"], 9)))
        passed = [c for c in checks if c["pillar"] == pillar and c["status"] in {"PASS", "NOT_APPLICABLE"}]
        value = scores[pillar]
        chips = "".join(kit.chip(f"{kit.STATUS[k][0]} {n}", kit.STATUS[k][1], kit.STATUS[k][2])
                        for k in kit.STATUS_ORDER for n in [value["counts"].get(k, 0)] if n)
        score_text = kit.num(value["score"]) if value["score"] is not None else "unassessed"
        body = "".join(finding_card(c) for c in items) or '<p class="small">No fail, warn or unknown checks.</p>'
        if passed:
            grouped = Counter((c["rule_id"], c["status"]) for c in passed)
            first = {(c["rule_id"], c["status"]): c["observation"] for c in reversed(passed)}
            rows = "".join(f'<tr><td><code>{kit.esc(rule)}</code></td><td>{kit.status_chip(state)}</td><td>{n}</td><td>{kit.esc(first[(rule, state)])}</td></tr>'
                           for (rule, state), n in sorted(grouped.items()))
            body += (f'<details><summary class="small">Passed and not applicable checks ({len(passed)})</summary><div class="body">'
                     f'<table><thead><tr><th>Rule</th><th>Status</th><th>Count</th><th>Observation (first instance)</th></tr></thead><tbody>{rows}</tbody></table></div></details>')
        pillar_sections.append(f'<details{" open" if items else ""}><summary>{kit.esc(pillar)} <span class="small">local score {kit.esc(score_text)}</span>{chips}</summary>'
                               f'<div class="body">{body}</div></details>')

    coverage = manifest.get("coverage", {})
    coverage_rows = "".join(f'<tr><td>{kit.esc(k)}</td><td><b>{kit.esc(v)}</b></td></tr>' for k, v in coverage.items())
    meta_chip = (f"model {run_meta['models_seen'][0]}" if run_meta and len(run_meta["models_seen"]) == 1 else ("model not recorded" if not run_meta else f"{len(run_meta['models_seen'])} models"))
    hero = kit.hero("AWS architecture review · Well-Architected six pillars", "AWS architecture review", status,
                    [f"account {inventory['account_id']}", ", ".join(inventory["regions"]), f"collection {manifest['status']}",
                     f"collected {str(manifest.get('completed_at', ''))[:16]}", f"{len(checks)} checks", meta_chip])
    nav = kit.toc([("glance", "At a glance"), ("architecture", "AS-IS architecture"), ("findings", "Findings"), ("provenance", "Run provenance"), ("scope", "Scope and limitations")])
    kpis = (kit.kpi(len(checks), "checks") + kit.kpi(counts["FAIL"], "fail", "fail") + kit.kpi(counts["WARN"], "warn", "warn")
            + kit.kpi(counts["UNKNOWN"], "unknown", "unknown") + kit.kpi(counts["PASS"], "pass", "pass")
            + kit.kpi(len(inventory["resources"]), "inventory resources", "link"))
    lowest = min(((p, v["score"]) for p, v in scores.items() if v["score"] is not None), key=lambda kv: kv[1], default=None)
    lowest_note = (f"<b>Lowest pillar:</b> {kit.esc(lowest[0])} at {kit.num(lowest[1])}. " if lowest else "<b>No pillar has a judged score yet.</b> ")
    basis_html = f'<div class="callout"><p>{kit.esc(basis_note)}</p></div>' if basis_note else ""
    figure = (f'<figure>{diagram_markup}<figcaption>AWS-styled diagram drawn from the collected configuration · geometry QA and audit recorded in diagrams/</figcaption></figure>'
              if diagram else f'<div class="callout"><p>{diagram_markup[3:-4] if diagram_markup.startswith("<p>") else kit.esc(diagram_markup)}'
                              ' - draw it with diagram.py before delivery.</p></div>')
    body = f"""{hero}{nav}<div class="wrap">
<section id="glance"><h2>At a glance</h2><p class="lead">Scores use the local starter rules (PASS 1 · WARN 0.5 · FAIL 0, weighted average) and are not official AWS Well-Architected Tool scores or HRI/MRI ratings. UNKNOWN checks are excluded from scores and only lower evidence coverage.</p>
<div class="kpis">{kpis}</div>
<div class="grid g2"><div class="card"><h3>Local pillar scores</h3><p class="sub">0-100 · lowest judged pillar highlighted · evidence coverage at right</p>{kit.score_bars(scores)}</div>
<div class="card"><h3>Judgement distribution</h3><p class="sub">{len(checks)} checks</p><div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap"><div style="width:200px;flex:0 0 200px">{kit.donut(counts)}</div><div>{kit.legend([(f"{k} · {counts.get(k, 0)}", kit.STATUS[k][1]) for k in kit.STATUS_ORDER])}</div></div></div></div>
<div class="grid g2" style="margin-top:16px"><div class="card"><h3>Judgements per pillar</h3><p class="sub">counts · resource-level checks can inflate pass counts</p>{kit.stacked_status(scores)}{kit.legend([(kit.STATUS[k][0], kit.STATUS[k][1]) for k in kit.STATUS_ORDER])}</div>
<div class="card"><h3>Fail and warn by severity</h3><p class="sub">local priority · not HRI/MRI</p>{kit.severity_bars(checks)}</div></div>
<div class="callout"><p>{lowest_note}Review the findings below for the evidence behind each judgement; a small inventory or a high score does not prove a healthy architecture.</p></div>{basis_html}</section>
<section id="architecture"><h2>AS-IS architecture</h2><p class="lead">Observed configuration only. Edges are configured paths (listener rules, security groups, route tables), not measured traffic.</p>{figure}
<div class="grid g2"><div class="card"><h3>Inventory by resource type</h3><p class="sub">{len(inventory["resources"])} resources · {", ".join(inventory["regions"])}</p>{kit.count_bars(inventory_pairs, "inventory-types", "Inventory by resource type", "Horizontal bars count collected resources per type.")}</div>
<div class="card"><h3>Collection coverage</h3><p class="sub">logical CLI invocations · pagination counted separately</p><table><tbody>{coverage_rows}<tr><td>truncations</td><td>{len(manifest.get("truncations", []))}</td></tr><tr><td>status</td><td>{kit.chip(manifest["status"], kit.ACCENT if manifest["status"] != "complete" else kit.STATUS["PASS"][1])}</td></tr></tbody></table>
<p class="small">Services in scope: {kit.esc(", ".join(inventory["services"]))}. Host internals, database contents, traffic and cost are outside the configuration snapshot.</p></div></div></section>
<section id="findings"><h2>Findings</h2><p class="lead">Fail, warn and unknown checks are listed first for each pillar; passed checks are grouped by rule. Confidence tags distinguish API configuration, template or source declarations and unverified conclusions.</p>{"".join(pillar_sections)}</section>
{kit.run_metadata_html(run_meta)}
<section id="scope"><h2>Scope and limitations</h2><ul><li>Collection scope: {kit.esc(", ".join(inventory["services"]))}. Automated starter checks and manual evidence-backed checks are shown together.</li>
<li>Query results: <code>{kit.esc(json.dumps(coverage, ensure_ascii=False))}</code> · detail limits: <code>{kit.esc(json.dumps(manifest.get("truncations", []), ensure_ascii=False))}</code>.</li>
<li>Actual traffic, code/database compatibility, SLOs, successful recovery, cost and business criticality require separate evidence.</li></ul></section>
<footer>findings.json · scorecard.json · review.md · diagrams/diagram-audit.json · local rule set {RULESET_VERSION}</footer></div>"""
    return kit.page("AWS architecture review", body)


def os_relative(path, root):
    import os
    return Path(os.path.relpath(path, root)).as_posix()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-is", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checks", type=Path, help="Optional evidence-backed replacement checks array (replaces the baseline)")
    parser.add_argument("--manual", type=Path, help="Agent-authored checks merged with the baseline (preferred over --checks)")
    parser.add_argument("--scope", type=Path, help="scope-resources.json; default: <as-is>/scope-resources.json when present")
    parser.add_argument("--run-scope", type=Path, help="The run's scope.json (workload name, context.environment); default: <as-is>/../scope.json when present")
    parser.add_argument("--diagram", type=Path)
    parser.add_argument("--run-metadata", type=Path, help="Agent-recorded run-metadata.json (model, inference time, tokens); see aws-mcp-setup run_metadata.py")
    args = parser.parse_args()
    try:
        inventory, manifest, calls = verify_inputs(args.as_is)
        run_meta = report_kit.load_run_metadata(args.run_metadata)
        scoped = load_scope(args.scope or args.as_is / "scope-resources.json", inventory)
        run_scope = load_run_scope(args.run_scope or args.as_is.parent / "scope.json", inventory)
        baseline = evaluate(inventory, manifest, calls, args.as_is, scoped, run_scope)
        if args.manual:
            manual = json.loads(args.manual.read_text(encoding="utf-8"))
            manual = manual["checks"] if isinstance(manual, dict) else manual
            language = manual_language_errors(manual)
            if language:
                raise ValueError(language[0] + f" ({len(language)} field(s))")
            checks = merge_manual(baseline, manual)
        else:
            checks = json.loads(args.checks.read_text(encoding="utf-8")) if args.checks else baseline
        validate_iac_changes(checks, args.as_is, calls)
        scores = score(checks, calls, {r["id"] for r in inventory["resources"]})
        baseline_scores = score(baseline, calls, {r["id"] for r in inventory["resources"]})
        basis = assessment_basis(inventory, baseline, checks)
        if args.diagram:
            read_svg(args.diagram)
        args.output.mkdir(parents=True, exist_ok=True)
        result = {"schema_version": "1.0", "run_id": inventory["run_id"], "account_id": inventory["account_id"],
                  "evaluated_at": datetime.now(timezone.utc).isoformat(), "engine": "moda-starter",
                  "collection_status": manifest["status"], "ruleset_version": RULESET_VERSION,
                  "scope": {"resources": len(scoped) if scoped is not None else len(inventory["resources"]),
                            "source": "scope-resources.json" if scoped is not None else "whole snapshot",
                            "environment": (run_scope or {}).get("environment", "unknown")},
                  "assessment_basis": "assessment-basis.json", "run_metadata": run_meta,
                  "as_is_snapshot": {"path": os_relative(args.as_is, args.output.parent),
                                     "resources": len(inventory["resources"]),
                                     "inventory_sha256": hashlib.sha256((args.as_is / "inventory.json").read_bytes()).hexdigest()},
                  "checks": checks}
        (args.output / "findings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        (args.output / "scorecard.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        for name, value in (("checks.baseline.json", baseline), ("scorecard.baseline.json", baseline_scores),
                            ("assessment-basis.json", basis)):
            (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        render_report(args.output, inventory, manifest, checks, scores, args.diagram, basis, run_meta)
        print(json.dumps({"status": "generated", "checks": len(checks), "counts": dict(Counter(c["status"] for c in checks)),
                          "diagram": "included" if args.diagram else "pending", "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"Review stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
