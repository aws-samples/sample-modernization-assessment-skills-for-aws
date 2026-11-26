#!/usr/bin/env python3
"""Specialist SA Agent reviews for stage 5: briefs, validation and merge. No AWS API calls.

Five Specialist SA Agents (sub-agents that take the role of an AWS specialist SA, or the main agent in turn when the client
has no sub-agent tool) each look at
the workload through one domain and write advice with a focused architecture diagram:

  containers  Container & Serverless Specialist SA Agent   container, serverless, managed-services, cloud-native (messaging options)
  database    Database Specialist SA Agent                 managed-database, open-source
  analytics   Data Analytics Specialist SA Agent           managed-analytics
  genai       GenAI Specialist SA Agent                    genai (AI options)
  devops      DevOps Specialist SA Agent                   modern-devops (observability options)

  python3 advice.py brief    --run-dir RUN [--specialist ID ...]   write 05-modernization/specialists/<id>/brief.md
  python3 advice.py validate --run-dir RUN --specialist ID         check advice.json, candidates.json and the diagram
  python3 advice.py merge    --run-dir RUN [--allow-missing]       specialist-advice.json, candidates.specialists.json,
                                                                   specialist list prices folded into list-prices.json
  python3 advice.py check-synthesis --run-dir RUN                  pathways.json statuses match the specialists' or say why

Advice text is written in the run's output language. The validator refuses invented references, amounts that did not
come from list_price.py, delivery durations, effect percentages, superlatives and imperative endings, so the section
reads as an architect's advice rather than a brochure.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
sys.path.insert(0, str(HERE))
import report_kit as kit  # noqa: E402

SPECIALISTS = {
    "containers": {"role": "Container & Serverless Specialist SA Agent", "pathways": ("container", "serverless", "managed-services", "cloud-native"),
                   "types": ("ecs.", "eks.", "ec2.instance", "autoscaling.", "elbv2.", "lambda.", "apigateway", "sqs.", "sns.", "events.", "stepfunctions.", "ecr.", "cloudfront.", "ssm."),
                   "rules": r"^(ECS_|EKS_|EC2_|ASG|ELB_|ALB_|LAMBDA|SQS|SNS|CONTAINER_|GRAVITON|COMPUTE_OPTIMIZER_FINDING|CUSTOMER_INGRESS|SUS_|PERF_)"},
    "database": {"role": "Database Specialist SA Agent", "pathways": ("managed-database", "open-source"),
                 "types": ("rds.", "elasticache.", "dynamodb.", "secretsmanager.", "backup."),
                 "rules": r"^(RDS_|AURORA_|ELASTICACHE_|CACHE_|DB_|DYNAMODB|BACKUP_|SECRET_|STACK_DATA_|REL_RESTORE|DATA_SG_|SCOPE_DYNAMODB|COMMERCIAL_LICENSE)"},
    "analytics": {"role": "Data Analytics Specialist SA Agent", "pathways": ("managed-analytics",),
                  "types": ("rds.", "dynamodb.", "s3.", "athena.", "glue.", "redshift.", "opensearch.", "kinesis.", "firehose.", "quicksight."),
                  "rules": r"^(ANALYTICS|ATHENA|GLUE|REDSHIFT|OPENSEARCH|KINESIS|S3_|AURORA_SINGLE|SCOPE_DYNAMODB|COST_WORKLOAD)"},
    "genai": {"role": "GenAI Specialist SA Agent", "pathways": ("genai",),
              "types": ("bedrock.", "sagemaker.", "ec2.instance", "lambda.", "s3."),
              "rules": r"^(GENAI_|BEDROCK|AI_|SAGEMAKER)"},
    "devops": {"role": "DevOps Specialist SA Agent", "pathways": ("modern-devops",),
               "types": ("codepipeline.", "codebuild.", "codecommit.", "ecr.", "cloudwatch.", "logs.", "sns.", "cloudformation.", "ecs.service", "eks.cluster", "xray."),
               "rules": r"^(CI_|DELIVERY_|IMAGE_TAG|ECR_|STACK_DRIFT|IAC_|ALARM_|LOG_|DASHBOARD_|OPS_|EC2_CLOUDWATCH|ECS_CONTAINER_INSIGHTS|ECS_DEPLOYMENT|EKS_OBSERVABILITY|EKS_CONTROL_PLANE|CLOUDTRAIL|VPC_FLOW)"},
}
ORDER = tuple(SPECIALISTS)
PATHWAY_OWNER = {p: sid for sid, s in SPECIALISTS.items() for p in s["pathways"]}
EFFORT = ("setting", "days", "weeks", "quarter")
COST = ("up", "neutral", "down", "measure_first")
KINDS = ("move", "improve")  # optional; the report's top recommendations put moves to a modern target first
STATES = {"retained", "proposed", "changed", "removal_candidate", "unresolved"}
REFERENCE_HOSTS = {"docs.aws.amazon.com", "aws.amazon.com", "repost.aws"}
ID_PREFIX = {"containers": "ct-", "database": "db-", "analytics": "da-", "genai": "ai-", "devops": "do-"}

# Wording the section must not use. Amounts come only from list_price.py ids; the size of a change is the effort word,
# not a calendar; effects are not quantified without a measurement; advice is suggested, not ordered or advertised.
FORBIDDEN = [
    (re.compile(r"[$\u20ac\u00a3\u20a9]\s?\d|\d[\d,.]*\s?USD\b"), "an amount in the text (cite list_prices ids instead)"),
    # A measurement window ("measure for two weeks") is advice; a delivery estimate ("done within two weeks") is not.
    (re.compile(r"(?i)\b(?:within|takes?|about|around)\s+(?:\d+|a|one|two|three|four|a few|several)\s+(?:hours?|days?|weeks?|months?|quarters?)\b"),
     "a delivery duration (use the effort word)"),
    (re.compile(r"(?i)(?:reduc|sav|improv|faster|lower|cut|increas|boost)\w*[^.]{0,30}?\d+(?:\.\d+)?\s?%"), "an unmeasured effect percentage"),
    (re.compile(r"(?i)\d+(?:\.\d+)?\s?%[^.]{0,20}?(?:reduction|improvement|faster|cheaper|savings?)"), "an unmeasured effect percentage"),
    (re.compile(r"(?i)\b(?:best-in-class|industry-leading|world-class|seamless(?:ly)?|revolutionary|cutting-edge|game-chang\w+|unlock\w*|supercharg\w+)\b"), "promotional wording"),
    (re.compile(r"(?i)\byou must\b"), "an order (suggest instead)"),
]


def language_patterns():
    """Wording rules for other output languages live in the aws-mcp-setup glossaries (`advice_patterns`), next to the
    phrases the localizer refuses, so this script stays English and every language is checked the same way."""
    out = []
    for path in sorted((SKILL_DIR.parent / "aws-mcp-setup/assets").glob("glossary.*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("advice_patterns", []):
                out.append((re.compile(item["pattern"]), item["why"]))
            # Terms the glossary replaces (a Korean review written with them would keep them: the localizer preserves
            # text that is already in the target language). Separator rules apply to translations only.
            for phrase, use in (data.get("avoid") or {}).items():
                if phrase.strip() and phrase not in (" · ", "+"):
                    out.append((re.compile(re.escape(phrase)), f"a term the glossary replaces ({use})"))
        except (ValueError, KeyError, re.error) as exc:
            raise ValueError(f"{path.name}: advice_patterns: {exc}")
    return out


FORBIDDEN += language_patterns()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def judged_snapshot(run):
    """The snapshot the review judged (findings.json as_is_snapshot), else 03-as-is-enriched, else 03-as-is."""
    findings = read_json(run / "04-review/findings.json")
    pointer = (findings.get("as_is_snapshot") or {}).get("path")
    if pointer and (run / pointer / "inventory.json").is_file():
        return run / pointer
    return run / "03-as-is-enriched" if (run / "03-as-is-enriched/inventory.json").is_file() else run / "03-as-is"


class Context:
    """Everything a validator needs to resolve references, loaded once per run."""
    def __init__(self, run):
        self.run = Path(run)
        self.snapshot = judged_snapshot(self.run)
        self.inventory = read_json(self.snapshot / "inventory.json")
        self.findings = read_json(self.run / "04-review/findings.json")
        manifest = self.snapshot / "collection-manifest.json"
        self.manifest = read_json(manifest) if manifest.is_file() else {}
        scope = self.snapshot / "scope-resources.json"
        self.scope_ids = {r["id"] for r in read_json(scope).get("resources", [])} if scope.is_file() else None
        self.scope = read_json(self.run / "scope.json") if (self.run / "scope.json").is_file() else {}
        self.resource_ids = {r["id"] for r in self.inventory.get("resources", [])}
        self.finding_ids = {c["id"] for c in self.findings.get("checks", [])}
        self.evidence_ids = {c["id"] for c in self.manifest.get("calls", [])} | {e for c in self.findings.get("checks", []) for e in c.get("evidence_ids") or []}
        self.stack_keys = {f"{x.get('StackName')}/{x.get('LogicalResourceId')}" for x in (read_json(scope).get("stack_resources", []) if scope.is_file() else [])}
        prices = self.run / "05-modernization/list-prices.json"
        self.price_ids = {p["id"] for p in read_json(prices).get("prices", [])} if prices.is_file() else set()
        self.user_names = iam_user_names(self.inventory, self.findings)

    def known(self, ref):
        return ref in self.finding_ids or ref in self.evidence_ids or ref in self.resource_ids

    def folder(self, sid):
        return self.run / "05-modernization/specialists" / sid


def in_domain(spec, resource):
    return any(resource["type"].startswith(t) for t in spec["types"])


def _get(d, *path):
    for key in path:
        if isinstance(d, dict):
            d = d.get(key)
        elif isinstance(d, list) and isinstance(key, int) and key < len(d):
            d = d[key]
        else:
            return None
    return d


def _short(value, limit=48):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


SKIP_FACT_KEYS = re.compile(r"(?i)(arn|id$|ids$|time|date|created|modified|updated|dns|endpoint|token|owner|account|hostedzone|resourceid)")


def resource_facts(resource):
    """The configuration values a specialist reads first, per resource type, as `key=value` strings. Unknown types fall
    back to the scalar top-level attributes. Nothing here is interpreted; the specialist judges."""
    a = resource.get("attributes") or {}
    o = resource.get("observations") or {}
    t = resource.get("type", "")
    facts = []

    def add(label, value):
        if value is not None and value != "" and value != []:
            facts.append(f"{label}={_short(value)}")
    if t == "rds.cluster":
        members = a.get("DBClusterMembers") or []
        add("engine", f"{a.get('Engine')} {a.get('EngineVersion')}"); add("mode", a.get("EngineMode")); add("multi_az", a.get("MultiAZ"))
        add("writers", sum(1 for m in members if m.get("IsClusterWriter"))); add("readers", sum(1 for m in members if not m.get("IsClusterWriter")))
        add("azs", a.get("AvailabilityZones")); add("backup_retention_days", a.get("BackupRetentionPeriod")); add("deletion_protection", a.get("DeletionProtection"))
        add("storage_encrypted", a.get("StorageEncrypted")); add("iam_auth", a.get("IAMDatabaseAuthenticationEnabled")); add("auto_minor_upgrade", a.get("AutoMinorVersionUpgrade"))
    elif t == "rds.instance":
        add("class", a.get("DBInstanceClass")); add("engine", f"{a.get('Engine')} {a.get('EngineVersion')}"); add("az", a.get("AvailabilityZone")); add("multi_az", a.get("MultiAZ"))
        add("cluster", a.get("DBClusterIdentifier")); add("performance_insights", a.get("PerformanceInsightsEnabled")); add("monitoring_interval_s", a.get("MonitoringInterval"))
        add("publicly_accessible", a.get("PubliclyAccessible")); add("storage", f"{a.get('AllocatedStorage')} GB {a.get('StorageType') or ''}".strip()); add("encrypted", a.get("StorageEncrypted"))
        add("backup_retention_days", a.get("BackupRetentionPeriod")); add("deletion_protection", a.get("DeletionProtection"))
    elif t.startswith("elasticache."):
        add("engine", f"{a.get('Engine')} {a.get('EngineVersion') or ''}".strip()); add("node_type", a.get("CacheNodeType")); add("nodes", a.get("NumCacheNodes") or len(a.get("MemberClusters") or []) or None)
        add("automatic_failover", a.get("AutomaticFailover")); add("multi_az", a.get("MultiAZ")); add("cluster_mode", a.get("ClusterMode")); add("at_rest_encryption", a.get("AtRestEncryptionEnabled"))
        add("transit_encryption", a.get("TransitEncryptionEnabled")); add("auth_token", a.get("AuthTokenEnabled")); add("snapshot_retention_days", a.get("SnapshotRetentionLimit")); add("az", a.get("PreferredAvailabilityZone"))
    elif t == "dynamodb.table":
        add("billing", _get(a, "BillingModeSummary", "BillingMode") or ("PROVISIONED" if (a.get("ProvisionedThroughput") or {}).get("ReadCapacityUnits") else None))
        add("items", a.get("ItemCount")); add("size_bytes", a.get("TableSizeBytes")); add("deletion_protection", a.get("DeletionProtectionEnabled")); add("sse", _get(a, "SSEDescription", "Status"))
        add("pitr", _get(o, "continuous_backups", "data", "ContinuousBackupsDescription", "PointInTimeRecoveryDescription", "PointInTimeRecoveryStatus"))
    elif t == "ecs.service":
        add("launch", a.get("launchType") or "capacity provider"); add("desired", a.get("desiredCount")); add("running", a.get("runningCount")); add("platform", a.get("platformVersion"))
        add("circuit_breaker", _get(a, "deploymentConfiguration", "deploymentCircuitBreaker", "enable")); add("rollback", _get(a, "deploymentConfiguration", "deploymentCircuitBreaker", "rollback"))
        add("controller", _get(a, "deploymentController", "type")); add("subnets", len(_get(a, "networkConfiguration", "awsvpcConfiguration", "subnets") or []) or None)
        add("public_ip", _get(a, "networkConfiguration", "awsvpcConfiguration", "assignPublicIp")); add("load_balancers", len(a.get("loadBalancers") or []) or None)
        add("health_grace_s", a.get("healthCheckGracePeriodSeconds")); add("exec_command", a.get("enableExecuteCommand")); add("task_definition", str(a.get("taskDefinition") or "").rsplit("/", 1)[-1])
    elif t == "ecs.cluster":
        add("services", a.get("activeServicesCount")); add("running_tasks", a.get("runningTasksCount")); add("capacity_providers", a.get("capacityProviders"))
        add("container_insights", next((x.get("value") for x in a.get("settings") or [] if x.get("name") == "containerInsights"), None))
    elif t == "eks.cluster":
        add("version", a.get("version")); add("platform", a.get("platformVersion")); add("support", _get(a, "upgradePolicy", "supportType"))
        add("endpoint_public", _get(a, "resourcesVpcConfig", "endpointPublicAccess")); add("endpoint_private", _get(a, "resourcesVpcConfig", "endpointPrivateAccess"))
        add("public_cidrs", _get(a, "resourcesVpcConfig", "publicAccessCidrs")); add("auth_mode", _get(a, "accessConfig", "authenticationMode"))
        logging = [x.get("types") for x in _get(a, "logging", "clusterLogging") or [] if x.get("enabled")]
        add("control_plane_logs", sum(logging, []) or "none"); add("deletion_protection", a.get("deletionProtection"))
    elif t == "ec2.instance":
        add("type", a.get("InstanceType")); add("arch", a.get("Architecture")); add("platform", a.get("PlatformDetails")); add("az", _get(a, "Placement", "AvailabilityZone"))
        add("state", _get(a, "State", "Name")); add("imdsv2", _get(a, "MetadataOptions", "HttpTokens")); add("instance_profile", str(_get(a, "IamInstanceProfile", "Arn") or "").rsplit("/", 1)[-1] or None)
        add("detailed_monitoring", _get(a, "Monitoring", "State")); add("ami", a.get("ImageId")); add("public_ip", bool(a.get("PublicIpAddress")) if "PublicIpAddress" in a else None); add("launched", str(a.get("LaunchTime") or "")[:10] or None)
    elif t == "autoscaling.group":
        add("min", a.get("MinSize")); add("max", a.get("MaxSize")); add("desired", a.get("DesiredCapacity")); add("instances", len(a.get("Instances") or []) or 0)
        add("azs", a.get("AvailabilityZones")); add("health_check", a.get("HealthCheckType")); add("mixed_instances", bool(a.get("MixedInstancesPolicy")))
    elif t == "lambda.function":
        add("runtime", a.get("Runtime")); add("memory_mb", a.get("MemorySize")); add("timeout_s", a.get("Timeout")); add("arch", a.get("Architectures")); add("package", a.get("PackageType"))
        add("vpc_subnets", len(_get(a, "VpcConfig", "SubnetIds") or []) or None)
    elif t == "elbv2.load-balancer":
        add("type", a.get("Type")); add("scheme", a.get("Scheme")); add("azs", [z.get("ZoneName") for z in a.get("AvailabilityZones") or []]); add("ip", a.get("IpAddressType"))
        listeners = _get(o, "listeners", "data", "Listeners") or []
        add("listeners", [f"{x.get('Protocol')}:{x.get('Port')}" for x in listeners] or None)
    elif t == "s3.bucket":
        add("versioning", _get(o, "versioning", "data", "Status") or ("off" if _get(o, "versioning", "status") == "ok" else None))
        rules = _get(o, "encryption", "data", "ServerSideEncryptionConfiguration", "Rules") or []
        add("encryption", _get(rules[0] if rules else {}, "ApplyServerSideEncryptionByDefault", "SSEAlgorithm"))
        pab = _get(o, "public_access_block", "data", "PublicAccessBlockConfiguration") or {}
        add("public_access_block", "all" if pab and all(pab.values()) else ("partial" if pab else None)); add("created", str(a.get("CreationDate") or "")[:10] or None)
    elif t == "ecr.repository":
        add("tag_mutability", a.get("imageTagMutability")); add("scan_on_push", _get(a, "imageScanningConfiguration", "scanOnPush")); add("encryption", _get(a, "encryptionConfiguration", "encryptionType"))
    elif t == "codepipeline.pipeline":
        add("type", a.get("pipelineType")); add("execution_mode", a.get("executionMode")); add("stages", [x.get("name") for x in a.get("stages") or []] or None)
    elif t == "secretsmanager.secret":
        add("rotation", a.get("RotationEnabled") if "RotationEnabled" in a else "not configured"); add("last_changed", str(a.get("LastChangedDate") or "")[:10] or None)
        add("last_accessed", str(a.get("LastAccessedDate") or "")[:10] or None); add("owning_service", a.get("OwningService"))
    elif t == "sns.topic":
        add("subscriptions_confirmed", a.get("SubscriptionsConfirmed")); add("subscriptions_pending", a.get("SubscriptionsPending"))
    elif t == "sqs.queue":
        add("visibility_timeout_s", a.get("VisibilityTimeout")); add("dead_letter", bool(a.get("RedrivePolicy"))); add("fifo", a.get("FifoQueue"))
    elif t == "cloudwatch.alarm":
        add("metric", f"{a.get('Namespace') or ''} {a.get('MetricName') or ''}".strip() or None); add("state", a.get("StateValue")); add("actions", len(a.get("AlarmActions") or []) or 0)
    elif t == "logs.group":
        add("retention_days", a.get("retentionInDays") or "never expires"); add("stored_bytes", a.get("storedBytes"))
    elif t == "events.rule":
        add("state", a.get("State")); add("schedule", a.get("ScheduleExpression")); add("event_pattern", bool(a.get("EventPattern")))
    else:
        for key, value in a.items():
            if isinstance(value, (str, int, float, bool)) and not SKIP_FACT_KEYS.search(key) and len(str(value)) <= 40:
                add(key, value)
            if len(facts) >= 8:
                break
    return facts


def dated_findings(checks):
    """Checks whose observation carries a calendar date (support ends, deprecation, expiry)."""
    out = []
    for c in checks:
        if c["status"] in {"FAIL", "WARN"}:
            dates = re.findall(r"\b(20\d\d-\d\d-\d\d)\b", str(c.get("observation", "")))
            if dates:
                out.append((min(dates), c))
    return sorted(out, key=lambda t: t[0])


def embedded(path, heading_level="##"):
    """A reference file's body for the brief, with its headings demoted one level so the brief keeps one outline."""
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"(?m)^(#{1,5}) ", lambda m: "#" * (len(m.group(1)) + 1) + " ", text)
    text = re.sub(r"\]\((?!https?://)([^)]+)\)", lambda m: "](" + str((SKILL_DIR / "references" / m.group(1)).resolve()) + ")", text)
    return text.strip()


def brief_text(ctx, sid):
    """The dossier a specialist works from: rules, domain guide, scope context, the resources of its domain with their
    key configuration values, the findings that touch them with full text, dated items, the current diagram's nodes
    and groups, list prices already looked up, its pathway drafts, a pre-filled advice.json skeleton and the commands.
    One file, so the review is one model call plus the helper commands."""
    spec = SPECIALISTS[sid]
    run = ctx.run
    folder = ctx.folder(sid)
    language = ctx.scope.get("output_language") or "en"
    profile = ((ctx.scope.get("auth") or {}).get("profile")) or "<profile>"
    workload = ctx.scope.get("workload_name") or ctx.inventory.get("workload") or "the workload"
    resources = [r for r in ctx.inventory.get("resources", []) if r.get("level") != "account" and (ctx.scope_ids is None or r["id"] in ctx.scope_ids) and in_domain(spec, r)]
    pattern = re.compile(spec["rules"])
    domain_ids = {r["id"] for r in resources}
    checks = [c for c in ctx.findings.get("checks", []) if c["status"] in {"FAIL", "WARN", "UNKNOWN"} and (pattern.search(c["rule_id"]) or c.get("resource_id") in domain_ids)]
    passed = [c for c in ctx.findings.get("checks", []) if c["status"] == "PASS" and (pattern.search(c["rule_id"]) or c.get("resource_id") in domain_ids)]
    review = SKILL_DIR.parent / "aws-architecture-review"
    pathways = ", ".join(f"`{p}`" for p in spec["pathways"])
    context = ctx.scope.get("context") or {}

    def cell(text):
        return str(text).replace("|", "/").replace("\n", " ")
    rows = "\n".join(f"| `{r['id']}` | {r['type']} | {cell(r.get('name') or r.get('native_id') or '')} | {cell(' · '.join(resource_facts(r)) or '-')} |" for r in resources[:120])
    more = f"\n\n({len(resources) - 120} more in the inventory; open it only for these.)" if len(resources) > 120 else ""
    frows = "\n".join(f"| {c['id']} | {c['rule_id']} | {c['status']} | {c.get('severity')} | `{cell(c.get('resource_id') or c.get('target') or '')}` | {cell(c.get('observation', ''))} | {cell(c.get('recommendation', ''))} | {', '.join(c.get('evidence_ids') or [])} |" for c in checks)
    prows = "\n".join(f"| {rule} | {n} |" for rule, n in sorted(__import__('collections').Counter(c['rule_id'] for c in passed).items()))
    dated = dated_findings(checks)
    drows = "\n".join(f"| {d} | {c['id']} | {c['rule_id']} | `{cell(c.get('resource_id') or '')}` | {cell(c.get('observation', ''))} |" for d, c in dated)
    graph_path = run / "04-review/diagrams/diagram-graph.json"
    graph = read_json(graph_path) if graph_path.is_file() else {"nodes": [], "groups": []}
    nrows = "\n".join(f"| {n['id']} | {cell(n.get('label', ''))} | {cell(n.get('detail') or '')} | {n.get('group') or ''} | {', '.join(f'`{x}`' for x in (n.get('resource_ids') or [])[:3])}{' +' + str(len(n.get('resource_ids') or []) - 3) if len(n.get('resource_ids') or []) > 3 else ''} |"
                       for n in graph.get("nodes", []) if set(n.get("resource_ids") or []) & domain_ids or any(str(n.get("type", "")).startswith(t) for t in spec["types"]))
    grows = "\n".join(f"| {g['id']} | {g.get('kind')} | {cell(g.get('label', ''))} | {g.get('parent') or ''} |" for g in graph.get("groups", []))
    prices_path = run / "05-modernization/list-prices.json"
    prices = read_json(prices_path).get("prices", []) if prices_path.is_file() else []
    price_rows = "\n".join(f"| {p['id']} | {cell(p.get('label', ''))} | {p.get('count')} | {p.get('monthly_usd')} |" for p in prices)
    drafts_path = run / "05-modernization/candidates.draft.json"
    drafts = read_json(drafts_path) if drafts_path.is_file() else []
    drafts = drafts.get("candidates", drafts) if isinstance(drafts, dict) else drafts
    mine = [c for c in drafts if c.get("pathway") in spec["pathways"]]
    skeleton = {"schema_version": "1.0", "specialist": sid, "summary": "", "current_state": [{"text": "", "refs": []}],
                "recommendations": [{"title": "", "pathway": spec["pathways"][0], "current": "", "change": "", "benefit": "", "refs": [], "kind": "move", "effort": "days", "cost_direction": "neutral",
                                     "list_prices": [], "before_deciding": [], "alternatives": [], "rollback": ""}],
                "not_now": [], "handoffs": [], "diagram": {"svg": "diagram.aws.svg", "caption": ""}, "references": []}
    scope_lines = [f"- Environment: **{context.get('environment', 'unknown')}**; operator: **{context.get('operator', 'unknown')}**; support plan: **{context.get('support_plan', 'unknown')}**; "
                   f"compliance: **{', '.join(context.get('compliance') or ['unknown'])}**. Known values are settled: do not ask for them again and do not hedge on them."]
    if ctx.scope.get("notes"):
        scope_lines.append(f"- Notes from the scope interview: {cell(ctx.scope['notes'])}")
    return f"""# Brief: {spec['role']} for {workload}

Run `{run}` · account {ctx.inventory.get('account_id')} · {', '.join(ctx.inventory.get('regions') or [])} · report language **{language}**
Write every sentence, title, diagram label and caption **in English**, whatever the report language: the finished report
is translated in one pass at the end when the language is not English, so no part of it mixes languages. The validator
refuses text in another language.

You cover the pathways {pathways}. **This file is your whole input.** The shared rules, your domain guide, the scope, the
resources of your domain with their configuration values, the findings that touch them, the current diagram's nodes and
the prices already looked up are all below. Do not open `inventory.json`, `findings.json` or the raw evidence; when a
value you need is not here, write the gap into `before_deciding` or `not_now` instead of guessing. Work in this order:
read once, write `advice.json`, `candidates.json` and `diagram-graph.json`, run the three commands at the end, fix what
they report, reply with the validator's output.

## Scope

{chr(10).join(scope_lines)}

## Resources in your domain ({len(resources)})

| ID | Type | Name | Configuration |
|---|---|---|---|
{rows or '| - | - | - | nothing in scope for this domain |'}{more}

## Findings that touch your domain ({len(checks)} failed, warned or open)

| ID | Rule | Status | Severity | Resource | Observation | Recommendation | Evidence |
|---|---|---|---|---|---|---|---|
{frows or '| - | - | - | - | - | nothing failed or warned | - | - |'}

Passed checks in your domain ({len(passed)}): {', '.join(f'{rule} × {n}' for rule, n in sorted(__import__('collections').Counter(c['rule_id'] for c in passed).items())) or 'none'}.
Cite a passed check when something is already in good shape.

## Dates the business cannot move ({len(dated)})

| Date | Finding | Rule | Resource | Observation |
|---|---|---|---|---|
{drows or '| - | - | - | - | none in your domain |'}

## Current diagram: nodes in your domain and every group

Reuse these labels and `resource_ids` for the `retained`, `changed` and `removal_candidate` nodes of your drawing.

| Node | Label | Detail | Group | Resource IDs |
|---|---|---|---|---|
{nrows or '| - | - | - | - | none |'}

| Group | Kind | Label | Parent |
|---|---|---|---|
{grows or '| - | - | - | - |'}

## List prices already looked up ({len(prices)})

| ID | Label | Count | USD per month |
|---|---|---|---|
{price_rows or '| - | none yet | - | - |'}

Cite these ids in `list_prices`; look up anything else with the command below, into your own file.

## Your pathway drafts (from candidates.draft.json)

```json
{json.dumps(mine, ensure_ascii=False, indent=1)}
```

## Write

1. `{folder}/advice.json`, starting from this skeleton (schema and field rules in the shared rules below):

   ```json
   {json.dumps(skeleton, ensure_ascii=False, indent=1)}
   ```

2. `{folder}/candidates.json`: a JSON array with exactly your pathway objects ({pathways}), in the scoring and output contract
   (`{SKILL_DIR}/references/scoring-and-output.md`), including the option list your pathway carries. Start from the drafts above.
3. `{folder}/diagram-graph.json`, then draw it (the renderer makes the AWS-styled SVG and checks its geometry; no redraw):

   ```bash
   python3 "{review}/scripts/diagram.py" --graph "{folder}/diagram-graph.json" --output "{folder}" --name diagram --title "<your role>: <what the drawing shows>" --language {language}
   ```

   Exit code 0 means drawn; otherwise fix the graph (shorter labels, a dangling edge) and run it again.

Prices: only from the read-only helper, into your own file so parallel reviewers do not collide:

```bash
python3 "{SKILL_DIR}/scripts/list_price.py" --profile "{profile}" --id-prefix {ID_PREFIX[sid]} --output "{folder}/list-prices.json" rds|ec2|elasticache|eks ... --count N --label "..."
```

## Check before you reply

```bash
python3 "{SKILL_DIR}/scripts/advice.py" validate --run-dir "{run}" --specialist {sid}
```

Fix what it reports (each error names the field) and run it again. Reply with its final output only.

---

# Shared rules for every Specialist SA Agent

{embedded(SKILL_DIR / "references/specialists.md")}

---

# Your domain guide

{embedded(SKILL_DIR / f"references/specialists/{sid}.md")}
"""


def brief(ctx, ids):
    out = []
    for sid in ids:
        folder = ctx.folder(sid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "brief.md").write_text(brief_text(ctx, sid), encoding="utf-8")
        out.append({"specialist": sid, "role": SPECIALISTS[sid]["role"], "brief": str(folder / "brief.md")})
    return out


def texts(advice):
    """(where, text) for every prose field the reader sees."""
    yield "summary", advice.get("summary", "")
    for i, item in enumerate(advice.get("current_state") or []):
        yield f"current_state[{i}]", item.get("text", "")
    for i, rec in enumerate(advice.get("recommendations") or []):
        for key in ("title", "current", "change", "benefit", "rollback"):
            if key == "current" and not rec.get(key):
                continue  # optional: the report falls back to the cited findings' observations
            yield f"recommendations[{i}].{key}", rec.get(key, "")
        for key in ("before_deciding", "alternatives"):
            for j, value in enumerate(rec.get(key) or []):
                yield f"recommendations[{i}].{key}[{j}]", value
    for i, item in enumerate(advice.get("not_now") or []):
        yield f"not_now[{i}]", item.get("text", "")
    for i, item in enumerate(advice.get("handoffs") or []):
        yield f"handoffs[{i}]", item.get("text", "")
    yield "diagram.caption", (advice.get("diagram") or {}).get("caption", "")


USER_ARN = re.compile(r"(?:arn:aws:iam::\d{12}:user/(?:[\w+=,.@-]+/)*|access-entry/[\w.-]+/user/\d{12}/)([A-Za-z][A-Za-z0-9+=,.@_-]{1,63})")
GENERIC_NAMES = {"admin", "administrator", "root", "user", "users", "test", "dev", "ops", "deploy", "deployer", "ci", "cd", "bot", "service", "readonly", "operator"}


def iam_user_names(*documents):
    """IAM user names in user ARNs and EKS access-entry ARNs of the evidence: advice must not name a person."""
    text = json.dumps(documents, ensure_ascii=False)
    return {m.group(1) for m in USER_ARN.finditer(text) if len(m.group(1)) >= 3 and m.group(1).lower() not in GENERIC_NAMES}


def people_errors(advice, names):
    errors = []
    for where, text in texts(advice):
        for name in names:
            if re.search(rf"(?<![A-Za-z0-9_@.+=/-]){re.escape(name)}(?![A-Za-z0-9_@+=-])", str(text)):
                errors.append(f"{where}: names the IAM user {name[0]}*** (write 'an IAM user with long-lived credentials' instead)")
    return errors


def wording_errors(advice):
    errors = []
    for where, text in texts(advice):
        for pattern, why in FORBIDDEN:
            if where.startswith("current_state") and "percentage" in why:
                continue  # an observed share ("27% of the tagged resources") is a fact, not a promised effect
            match = pattern.search(str(text))
            if match:
                errors.append(f"{where}: {why}: {match.group(0)!r}")
    return errors


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_advice(ctx, sid, advice, price_ids):
    errors = []
    spec = SPECIALISTS[sid]
    if advice.get("specialist") != sid:
        errors.append(f"specialist must be {sid!r}")
    if not nonempty(advice.get("summary")):
        errors.append("summary is required (two to four sentences: what you see and what you would raise first)")
    state = advice.get("current_state")
    if not isinstance(state, list) or not state:
        errors.append("current_state needs at least one item")
    for i, item in enumerate(state or []):
        if not isinstance(item, dict) or not nonempty(item.get("text")) or not item.get("refs"):
            errors.append(f"current_state[{i}] needs text and refs")
        for ref in (item or {}).get("refs") or []:
            if not ctx.known(ref):
                errors.append(f"current_state[{i}] cites unknown {ref!r}")
    recs = advice.get("recommendations")
    if not isinstance(recs, list) or len(recs) > 4:
        errors.append("recommendations must be a list of at most four (say 'not now' instead of padding)")
    for i, rec in enumerate(recs or []):
        if not isinstance(rec, dict):
            errors.append(f"recommendations[{i}] must be an object"); continue
        for key in ("title", "change", "benefit", "rollback"):
            if not nonempty(rec.get(key)):
                errors.append(f"recommendations[{i}].{key} is required")
        if rec.get("pathway") not in spec["pathways"]:
            errors.append(f"recommendations[{i}].pathway must be one of {', '.join(spec['pathways'])}")
        if rec.get("effort") not in EFFORT or rec.get("cost_direction") not in COST:
            errors.append(f"recommendations[{i}] needs effort ({'/'.join(EFFORT)}) and cost_direction ({'/'.join(COST)})")
        if rec.get("kind") is not None and rec.get("kind") not in KINDS:
            errors.append(f"recommendations[{i}].kind must be {' or '.join(KINDS)} (move = the component runs somewhere or on something new; improve = the current setup gets better)")
        if not rec.get("refs") or any(not ctx.known(r) for r in rec.get("refs") or []):
            errors.append(f"recommendations[{i}].refs must cite existing findings, evidence or resources")
        if not isinstance(rec.get("alternatives"), list) or not [a for a in rec.get("alternatives") or [] if nonempty(a)]:
            errors.append(f"recommendations[{i}].alternatives needs at least one (keeping and improving the current setup counts)")
        if not isinstance(rec.get("before_deciding", []), list):
            errors.append(f"recommendations[{i}].before_deciding must be a list")
        if rec.get("current") is not None and not isinstance(rec.get("current"), str):
            errors.append(f"recommendations[{i}].current must be one sentence (a string) when given")
        if "evidence" in rec:
            errors.append(f"recommendations[{i}].evidence is written by benefits.py from the collected evidence, not by the review")
        for ref in rec.get("list_prices") or []:
            if not isinstance(ref, dict) or ref.get("sign") not in ("+", "-") or ref.get("id") not in price_ids:
                errors.append(f"recommendations[{i}].list_prices: {ref!r} is not a price list_price.py returned")
    for i, item in enumerate(advice.get("not_now") or []):
        if not isinstance(item, dict) or not nonempty(item.get("text")):
            errors.append(f"not_now[{i}] needs text")
        for ref in (item or {}).get("refs") or []:
            if not ctx.known(ref):
                errors.append(f"not_now[{i}] cites unknown {ref!r}")
    for i, item in enumerate(advice.get("handoffs") or []):
        if not isinstance(item, dict) or item.get("to") not in SPECIALISTS or item.get("to") == sid or not nonempty(item.get("text")):
            errors.append(f"handoffs[{i}] needs to (another specialist id) and text")
    for ref in advice.get("references") or []:
        url = ref.get("url", "") if isinstance(ref, dict) else ""
        host = urlparse(url).hostname or ""
        if not (url.startswith("https://") and nonempty(ref.get("title")) and (host in REFERENCE_HOSTS or host.endswith(".docs.aws.amazon.com"))):
            errors.append(f"reference {ref!r} must be an https AWS documentation page with a title")
    errors += wording_errors(advice)
    errors += people_errors(advice, getattr(ctx, "user_names", set()))
    errors += kit.english_errors(list(texts(advice)) + [("diagram.caption", (advice.get("diagram") or {}).get("caption", ""))])
    return errors


def validate_graph(ctx, graph, advice):
    errors = []
    groups = {g.get("id") for g in graph.get("groups", []) or []}
    nodes = graph.get("nodes") or []
    ids = [n.get("id") for n in nodes]
    edge_ids = [e.get("id") for e in graph.get("edges") or []]
    if not nodes:
        errors.append("diagram-graph.json has no nodes")
    if len(set(ids)) != len(ids) or len(set(edge_ids)) != len(edge_ids) or groups & set(ids):
        errors.append("diagram graph IDs must be unique")
    for g in graph.get("groups", []) or []:
        if g.get("parent") and g["parent"] not in groups:
            errors.append(f"group {g.get('id')} has unknown parent")
    for n in nodes:
        if n.get("state") not in STATES:
            errors.append(f"node {n.get('id')} state must be one of {', '.join(sorted(STATES))}")
        if n.get("group") and n["group"] not in groups:
            errors.append(f"node {n.get('id')} is in unknown group")
        if n.get("state") in {"retained", "changed", "removal_candidate"}:
            refs = list(n.get("resource_ids") or []) + list(n.get("stack_resources") or [])
            if not refs:
                errors.append(f"node {n.get('id')} ({n.get('state')}) is an existing component and needs resource_ids or stack_resources")
            for r in n.get("resource_ids") or []:
                if r not in ctx.resource_ids:
                    errors.append(f"node {n.get('id')} cites unknown resource {r!r}")
        if n.get("state") == "proposed" and n.get("resource_ids"):
            errors.append(f"node {n.get('id')} is proposed and cannot claim existing resource_ids")
    for e in graph.get("edges") or []:
        if e.get("source") not in ids or e.get("target") not in ids:
            errors.append(f"edge {e.get('id')} is dangling")
    if (advice.get("recommendations") or []) and not any(n.get("state") in {"proposed", "changed", "removal_candidate"} for n in nodes):
        errors.append("the diagram shows no change although recommendations exist (mark proposed, changed or removal_candidate)")
    return errors


def validate_svg(folder, graph, name):
    path = folder / name
    if not path.is_file():
        return [f"diagram file {name} is missing"], False
    text = path.read_text(encoding="utf-8")
    try:
        elements = kit.scan_svg(text)
    except ValueError as exc:
        return [f"{name}: {exc}"], False
    if name.endswith(".d2.svg"):
        return [], True  # the initial render: accepted, labelled as such in the report
    found = {kind: {a[f"data-{kind}-id"] for _, a in elements if f"data-{kind}-id" in a} for kind in ("node", "edge", "group")}
    missing = sorted({n["id"] for n in graph.get("nodes") or []} - found["node"])
    errors = [f"{name} lacks data-node-id for {', '.join(missing[:8])}"] if missing else []
    return errors, False


def validate_candidates(ctx, sid, fragment):
    spec = SPECIALISTS[sid]
    if not isinstance(fragment, list) or sorted(c.get("pathway") for c in fragment if isinstance(c, dict)) != sorted(spec["pathways"]):
        return [f"candidates.json must hold exactly the pathways {', '.join(spec['pathways'])}"]
    import recommend  # noqa: E402
    others = [c for c in recommend.bootstrap() if c["pathway"] not in spec["pathways"]]
    try:
        recommend.rank(others + fragment, ctx.resource_ids, ctx.finding_ids)
    except (ValueError, KeyError, TypeError) as exc:
        return [f"candidates.json: {exc}"]
    return kit.english_errors(candidate_texts(fragment, "candidates.json"))


def candidate_texts(candidates, where):
    """(where, text) for every string in pathway objects (problem, rationale, alternatives, criteria rationales, options)."""
    def walk(value, path):
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, dict):
            for k, v in value.items():
                yield from walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from walk(v, f"{path}[{i}]")
    for c in candidates if isinstance(candidates, list) else []:
        yield from walk(c, f"{where}[{c.get('pathway', '?') if isinstance(c, dict) else '?'}]")


def validate(ctx, sid):
    folder = ctx.folder(sid)
    errors, initial = [], False
    if not (folder / "advice.json").is_file():
        return {"specialist": sid, "status": "missing", "errors": ["advice.json not written"]}
    try:
        advice = read_json(folder / "advice.json")
    except (ValueError, OSError) as exc:
        return {"specialist": sid, "status": "fail", "errors": [f"advice.json: {exc}"]}
    local_prices = folder / "list-prices.json"
    price_ids = set(ctx.price_ids) | ({p["id"] for p in read_json(local_prices).get("prices", [])} if local_prices.is_file() else set())
    errors += validate_advice(ctx, sid, advice, price_ids)
    if (folder / "candidates.json").is_file():
        errors += validate_candidates(ctx, sid, read_json(folder / "candidates.json"))
    else:
        errors.append("candidates.json not written")
    if (folder / "diagram-graph.json").is_file():
        graph = read_json(folder / "diagram-graph.json")
        errors += validate_graph(ctx, graph, advice)
        name = (advice.get("diagram") or {}).get("svg") or "diagram.aws.svg"
        if Path(name).name != name:
            errors.append("diagram.svg must be a file name in the specialist folder")
        else:
            svg_errors, initial = validate_svg(folder, graph, name)
            errors += svg_errors
        if not nonempty((advice.get("diagram") or {}).get("caption")):
            errors.append("diagram.caption is required (what the drawing shows and what is proposed)")
    else:
        errors.append("diagram-graph.json not written")
    return {"specialist": sid, "role": SPECIALISTS[sid]["role"], "status": "fail" if errors else "pass", "errors": errors,
            "initial_render_only": initial, "recommendations": len(advice.get("recommendations") or [])}


def merge(ctx, allow_missing=False):
    results = [validate(ctx, sid) for sid in ORDER]
    present = [r for r in results if r["status"] != "missing"]
    failed = [r for r in present if r["status"] == "fail"]
    missing = [r["specialist"] for r in results if r["status"] == "missing"]
    if failed or (missing and not allow_missing) or not present:
        return {"status": "fail", "results": results,
                "note": "every specialist must pass; a review that cannot be done is written by the main agent, or merge with --allow-missing and say why in the report"}
    prices_path = ctx.run / "05-modernization/list-prices.json"
    prices = read_json(prices_path) if prices_path.is_file() else {"schema_version": "1.0", "source": "AWS Price List API (pricing get-products)", "prices": []}
    known = {p["id"] for p in prices["prices"]}
    specialists, fragments, owners = [], [], {}
    for r in present:
        sid = r["specialist"]
        folder = ctx.folder(sid)
        advice = read_json(folder / "advice.json")
        local = folder / "list-prices.json"
        for p in (read_json(local).get("prices", []) if local.is_file() else []):
            if p["id"] not in known:
                prices["prices"].append(p); known.add(p["id"])
        graph = read_json(folder / "diagram-graph.json")
        specialists.append(dict(advice, role=SPECIALISTS[sid]["role"], pathways=list(SPECIALISTS[sid]["pathways"]),
                                diagram=dict(advice.get("diagram") or {}, svg=f"specialists/{sid}/{(advice.get('diagram') or {}).get('svg') or 'diagram.aws.svg'}",
                                             initial_render_only=r["initial_render_only"],
                                             changes=sum(1 for n in graph.get("nodes", []) if n.get("state") in {"proposed", "changed", "removal_candidate"}))))
        for c in read_json(folder / "candidates.json"):
            fragments.append(dict(c, specialist=sid))
        for rec in advice.get("recommendations") or []:
            for ref in rec.get("refs") or []:
                owners.setdefault(ref, set()).add(sid)
    import recommend  # noqa: E402
    covered = {c["pathway"] for c in fragments}
    evidence = recommend.evidence_context(ctx.inventory, ctx.findings.get("checks", []), ctx.scope_ids, ctx.run, ctx.snapshot, specialists)
    combined = fragments + [recommend.inserted_pathway(c["pathway"], evidence) if c["pathway"] in recommend.ADDED_PATHWAYS else c
                            for c in recommend.bootstrap() if c["pathway"] not in covered]
    combined.sort(key=lambda c: list(recommend.PATHWAYS).index(c["pathway"]))
    out = ctx.run / "05-modernization"
    prices_path.parent.mkdir(parents=True, exist_ok=True)
    prices_path.write_text(json.dumps(prices, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "specialist-advice.json").write_text(json.dumps({"schema_version": "1.0", "run_id": ctx.inventory.get("run_id"), "account_id": ctx.inventory.get("account_id"),
                                                            "specialists": specialists}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "candidates.specialists.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shared = {ref: sorted(s) for ref, s in owners.items() if len(s) > 1}
    return {"status": "merged", "specialists": [s["specialist"] for s in specialists], "missing": [r["specialist"] for r in results if r["status"] == "missing"],
            "statuses": {c["pathway"]: c["status"] for c in fragments}, "shared_references": shared,
            "next": "Resolve shared references (one owner per change), then write candidates.json from candidates.specialists.json and run recommend.py --candidates."}


def check_synthesis(ctx):
    advice = ctx.run / "05-modernization/specialist-advice.json"
    pathways = ctx.run / "05-modernization/pathways.json"
    if not advice.is_file() or not pathways.is_file():
        return {"status": "fail", "errors": ["run advice.py merge and recommend.py --candidates first"]}
    proposed = {}
    for s in read_json(advice)["specialists"]:
        folder = ctx.folder(s["specialist"])
        for c in read_json(folder / "candidates.json"):
            proposed[c["pathway"]] = (s["specialist"], c["status"])
    errors = []
    for c in read_json(pathways)["candidates"]:
        if c["pathway"] in proposed and proposed[c["pathway"]][1] != c["status"] and not nonempty(c.get("synthesis_note")):
            errors.append(f"{c['pathway']}: the {proposed[c['pathway']][0]} specialist proposed {proposed[c['pathway']][1]}, the synthesis has {c['status']}; "
                          "add synthesis_note saying why")
    return {"status": "fail" if errors else "pass", "errors": errors}


def load_for_report(run, finding_ids, evidence_ids, resource_ids, price_ids):
    """specialist-advice.json for the unified report, re-checked: every reference resolves and the wording rules hold."""
    path = Path(run) / "05-modernization/specialist-advice.json"
    if not path.is_file():
        return []
    doc = read_json(path)

    class _Ctx:
        def known(self, ref):
            return ref in finding_ids or ref in evidence_ids or ref in resource_ids
    out = []
    for s in doc.get("specialists", []):
        sid = s.get("specialist")
        if sid not in SPECIALISTS:
            raise ValueError(f"specialist-advice.json names an unknown specialist {sid!r}")
        errors = validate_advice(_Ctx(), sid, s, price_ids)
        if errors:
            raise ValueError(f"specialist advice ({sid}) fails validation: {errors[0]}")
        out.append(s)
    out.sort(key=lambda s: ORDER.index(s["specialist"]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("brief", "validate", "merge", "check-synthesis"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        if name in ("brief", "validate"):
            p.add_argument("--specialist", action="append", choices=ORDER, required=name == "validate")
        if name == "merge":
            p.add_argument("--allow-missing", action="store_true", help="merge the reviews that exist; the missing pathways stay not_assessed")
    args = parser.parse_args()
    try:
        ctx = Context(args.run_dir)
        if args.command == "brief":
            result = {"status": "written", "briefs": brief(ctx, args.specialist or list(ORDER))}
        elif args.command == "validate":
            results = [validate(ctx, sid) for sid in args.specialist]
            result = results[0] if len(results) == 1 else {"status": "fail" if any(r["status"] != "pass" for r in results) else "pass", "results": results}
        elif args.command == "merge":
            result = merge(ctx, args.allow_missing)
        else:
            result = check_synthesis(ctx)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result.get("status") in {"written", "pass", "merged"} else 1
    except (ValueError, OSError, KeyError) as exc:
        print(f"Specialist advice stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
