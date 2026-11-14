"""Baseline checks that read the supplementary workload evidence (supplement.py) as well as the inventory.

Each rule is deterministic, judges only what the recorded API responses show, and names its target. They
cover the gaps that agents previously filled with ad-hoc scripts: listener TLS on every load balancer in
scope (including ones created outside CloudFormation), IAM wildcard permissions in workload role policies,
alarm notification targets without subscribers, ECR/ECS/EKS/ElastiCache/Aurora/Secrets Manager settings and
Bedrock invocation logging and guardrails. Agents may override a status or severity in checks.manual.json
with an assessment_reason; they should not need to re-author these checks.
"""
import json
import re
import time
from urllib.parse import unquote

SH = "https://docs.aws.amazon.com/securityhub/latest/userguide/"


class Evidence:
    """Index of successful supplementary records and their payloads."""

    def __init__(self, root, calls):
        self.root, self.calls = root, calls

    def records(self, service, operation):
        return [c for c in self.calls.values() if c.get("service") == service and c.get("operation") == operation
                and c.get("status") == "ok" and c.get("raw_path")]

    def data(self, call):
        try:
            return json.loads((self.root / call["raw_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def arg(self, call, flag):
        args = call.get("arguments", [])
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def statements(document):
    if isinstance(document, str):
        try:
            document = json.loads(unquote(document))
        except ValueError:
            return []
    items = (document or {}).get("Statement", [])
    return items if isinstance(items, list) else [items]


def as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def evaluate_workload(emit, resources, evidence, scoped):
    """emit(rule, pillar, resource, status, observation, recommendation, url, weight, severity, evidence, target)."""
    typed = lambda kind: [r for r in resources if r["type"] == kind and r["id"] in scoped]  # noqa: E731
    by_native = {}
    for r in resources:
        by_native.setdefault(r["native_id"], r)

    # Listener TLS: every application/network LB in scope, including controller-created ones.
    for lb in typed("elbv2.load-balancer"):
        a = lb["attributes"]
        listeners = (lb["observations"].get("listeners", {}).get("data") or {}).get("Listeners")
        if listeners is None:
            continue
        plain = [l for l in listeners if l.get("Protocol") in {"HTTP", "TCP"} and not all(
            (act.get("Type") == "redirect" and (act.get("RedirectConfig") or {}).get("Protocol") == "HTTPS")
            for act in l.get("DefaultActions", []))]
        public = a.get("Scheme") == "internet-facing"
        ports = ", ".join(f"{l.get('Protocol')}:{l.get('Port')}" for l in plain)
        if plain:
            emit("ELB_TLS_LISTENER", "Security", lb, "FAIL" if public else "WARN",
                 f"{a.get('Scheme')} load balancer {a.get('LoadBalancerName')} serves {ports} without TLS or an HTTPS redirect.",
                 "Add an HTTPS listener with an ACM certificate (ACM Private CA for internal names) and redirect or close plain HTTP once clients use TLS.",
                 SH + "elb-controls.html#elb-1", weight=3, severity="MEDIUM")
        else:
            emit("ELB_TLS_LISTENER", "Security", lb, "PASS", f"All listeners of {a.get('LoadBalancerName')} use TLS or redirect to HTTPS.",
                 "Keep TLS policies current.", SH + "elb-controls.html#elb-1", weight=3, severity="MEDIUM")
    for call in evidence.records("elbv2", "describe-load-balancer-attributes"):
        lb = by_native.get(evidence.arg(call, "--load-balancer-arn"))
        if not lb or lb["id"] not in scoped:
            continue
        attrs = {x.get("Key"): x.get("Value") for x in (evidence.data(call) or {}).get("Attributes", [])}
        ev = lb["evidence_ids"] + [call["id"]]
        if lb["attributes"].get("Type") == "application" and "routing.http.drop_invalid_header_fields.enabled" in attrs:
            ok = attrs["routing.http.drop_invalid_header_fields.enabled"] == "true"
            emit("ELB_DROP_INVALID_HEADERS", "Security", lb, "PASS" if ok else "WARN",
                 f"routing.http.drop_invalid_header_fields.enabled={attrs['routing.http.drop_invalid_header_fields.enabled']}.",
                 "Set the attribute to true (Security Hub ELB.4) after checking that no client depends on invalid headers.",
                 SH + "elb-controls.html#elb-4", severity="LOW", evidence=ev)
        if "access_logs.s3.enabled" in attrs:
            ok = attrs["access_logs.s3.enabled"] == "true"
            emit("ELB_ACCESS_LOGS", "Operational Excellence", lb, "PASS" if ok else "WARN",
                 f"access_logs.s3.enabled={attrs['access_logs.s3.enabled']}; request-level evidence is {'kept' if ok else 'not kept'}.",
                 "Enable access logs to an S3 bucket with a lifecycle rule (Security Hub ELB.5).",
                 SH + "elb-controls.html#elb-5", severity="LOW", evidence=ev)

    # IAM wildcard permissions in workload role policies (inline and customer managed).
    docs = [(c, (evidence.data(c) or {}).get("PolicyDocument")) for c in evidence.records("iam", "get-role-policy")]
    docs += [(c, ((evidence.data(c) or {}).get("PolicyVersion") or {}).get("Document")) for c in evidence.records("iam", "get-policy-version")]
    for call, document in docs:
        role = call.get("provenance", {}).get("origin") or evidence.arg(call, "--role-name")
        broad, scoped_wild = [], []
        for st in statements(document):
            if st.get("Effect") != "Allow":
                continue
            actions = [str(x) for x in as_list(st.get("Action"))]
            targets = [str(x) for x in as_list(st.get("Resource"))]
            wild = [x for x in actions if x == "*" or x.endswith(":*")]
            if not wild:
                continue
            (broad if "*" in targets else scoped_wild).append(f"{', '.join(wild)} on {', '.join(targets[:3])}")
        if not (broad or scoped_wild):
            continue
        resource = next((r for r in resources if r["id"] in call.get("resource_ids", [])), None)
        name = evidence.arg(call, "--policy-name") or evidence.arg(call, "--policy-arn") or "policy"
        emit("IAM_WILDCARD_PERMISSION", "Security", resource,
             "FAIL" if broad else "WARN",
             f"Role {role} policy {name} allows {'; '.join(broad + scoped_wild)}.",
             "Replace service-wide wildcards with the actions the workload calls and the specific resource ARNs; generate a starting policy from CloudTrail with IAM Access Analyzer.",
             "https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html#grant-least-privilege",
             weight=4 if broad else 2, severity="HIGH" if broad else "MEDIUM", evidence=[call["id"]], target=f"{role} · {name}")

    # Alarms that notify a topic nobody subscribes to.
    subscriptions = {}
    for call in evidence.records("sns", "list-subscriptions-by-topic"):
        subscriptions[evidence.arg(call, "--topic-arn")] = (call, len((evidence.data(call) or {}).get("Subscriptions", [])))
    alarms = typed("cloudwatch.alarm")
    for topic, (call, count) in subscriptions.items():
        users = [a for a in alarms if topic in (a["attributes"].get("AlarmActions") or []) + (a["attributes"].get("OKActions") or [])]
        resource = by_native.get(topic)
        if not users:
            continue
        names = ", ".join(a["name"] for a in users[:5])
        emit("ALARM_NOTIFICATION_TARGET", "Operational Excellence", resource if resource and resource["id"] in scoped else None,
             "PASS" if count else "FAIL",
             f"{len(users)} alarm(s) ({names}) notify {topic.rsplit(':', 1)[-1]}, which has {count} subscription(s)." +
             ("" if count else " An alarm that fires reaches nobody."),
             "Subscribe the on-call channel (email, chat or incident tool) and send a test notification to confirm delivery.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html",
             weight=4, severity="HIGH", evidence=[call["id"]] + [e for a in users for e in a["evidence_ids"]], target=topic)

    # ECR repository settings.
    for call in evidence.records("ecr", "describe-repositories"):
        for repo in (evidence.data(call) or {}).get("repositories", []):
            target = repo.get("repositoryArn") or repo.get("repositoryName")
            scan = (repo.get("imageScanningConfiguration") or {}).get("scanOnPush")
            emit("ECR_SCAN_ON_PUSH", "Security", None, "PASS" if scan else "WARN",
                 f"{repo.get('repositoryName')}: scanOnPush={scan}. Registry-level scanning (Amazon Inspector) is a separate setting.",
                 "Turn on enhanced (continuous) scanning in the registry or scan on push, and gate deployments on the severity you accept.",
                 SH + "ecr-controls.html#ecr-1", severity="MEDIUM", evidence=[call["id"]], target=target)
            mutable = repo.get("imageTagMutability") == "MUTABLE"
            emit("ECR_TAG_IMMUTABILITY", "Security", None, "WARN" if mutable else "PASS",
                 f"{repo.get('repositoryName')}: imageTagMutability={repo.get('imageTagMutability')}.",
                 "Set IMMUTABLE and deploy by commit SHA or digest so a tag always means the same image (Security Hub ECR.2).",
                 SH + "ecr-controls.html#ecr-2", severity="MEDIUM", evidence=[call["id"]], target=target)

    # ECS services and clusters.
    for svc in typed("ecs.service"):
        cb = ((svc["attributes"].get("deploymentConfiguration") or {}).get("deploymentCircuitBreaker") or {})
        if cb:
            ok = cb.get("enable") and cb.get("rollback")
            emit("ECS_DEPLOYMENT_CIRCUIT_BREAKER", "Reliability", svc, "PASS" if ok else "WARN",
                 f"{svc['name']}: circuit breaker enable={cb.get('enable')}, rollback={cb.get('rollback')}. A failed deployment {'rolls back' if ok else 'is not rolled back automatically'}.",
                 "Enable the deployment circuit breaker with rollback and add container health checks.",
                 "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html", severity="MEDIUM")
    for cluster in typed("ecs.cluster"):
        settings = {s.get("name"): s.get("value") for s in cluster["attributes"].get("settings", [])}
        if "containerInsights" in settings:
            ok = settings["containerInsights"] in {"enabled", "enhanced"}
            emit("ECS_CONTAINER_INSIGHTS", "Operational Excellence", cluster, "PASS" if ok else "WARN",
                 f"{cluster['name']}: containerInsights={settings['containerInsights']}.",
                 "Turn on Container Insights (enhanced observability) for task-level CPU, memory and restart metrics.",
                 SH + "ecs-controls.html#ecs-12", severity="LOW")

    # EKS clusters.
    for eks in typed("eks.cluster"):
        a = eks["attributes"]
        logging = (a.get("logging") or {}).get("clusterLogging") or []
        if logging:
            enabled = {t for entry in logging if entry.get("enabled") for t in entry.get("types", [])}
            ok = {"audit", "authenticator"} <= enabled
            emit("EKS_CONTROL_PLANE_LOGGING", "Security", eks, "PASS" if ok else "FAIL",
                 f"{eks['name']}: control plane log types enabled: {', '.join(sorted(enabled)) or 'none'}.",
                 "Enable at least audit and authenticator logs with a retention period (Security Hub EKS.8).",
                 SH + "eks-controls.html#eks-8", severity="MEDIUM")
        vpc = a.get("resourcesVpcConfig") or {}
        if "endpointPublicAccess" in vpc:
            open_world = vpc.get("endpointPublicAccess") and "0.0.0.0/0" in (vpc.get("publicAccessCidrs") or [])
            emit("EKS_PUBLIC_ENDPOINT", "Security", eks, "WARN" if open_world else "PASS",
                 f"{eks['name']}: endpointPublicAccess={vpc.get('endpointPublicAccess')}, publicAccessCidrs={vpc.get('publicAccessCidrs')}.",
                 "Use the private endpoint, or restrict publicAccessCidrs to known networks (Security Hub EKS.1).",
                 SH + "eks-controls.html#eks-1", severity="MEDIUM")

    # ElastiCache replication groups.
    for call in evidence.records("elasticache", "describe-replication-groups"):
        for group in (evidence.data(call) or {}).get("ReplicationGroups", []):
            target, gid = group.get("ARN") or group.get("ReplicationGroupId"), group.get("ReplicationGroupId")
            tls, auth = group.get("TransitEncryptionEnabled"), group.get("AuthTokenEnabled")
            emit("ELASTICACHE_TRANSIT_ENCRYPTION", "Security", None, "PASS" if tls else "FAIL",
                 f"{gid}: TransitEncryptionEnabled={tls}, AuthTokenEnabled={auth}." + ("" if tls else " Clients in the allowed network read and write the cache in plain text."),
                 "Enable in-transit encryption (preferred, then required mode) and AUTH or RBAC users; update clients to TLS.",
                 SH + "elasticache-controls.html#elasticache-5", weight=3, severity="HIGH" if not (tls or auth) else "MEDIUM",
                 evidence=[call["id"]], target=target)
            members = sum(len(n.get("NodeGroupMembers", [])) for n in group.get("NodeGroups", []))
            multi = group.get("MultiAZ") == "enabled" and group.get("AutomaticFailover") == "enabled"
            emit("ELASTICACHE_MULTI_AZ", "Reliability", None, "PASS" if multi else "WARN",
                 f"{gid}: {members} node(s), MultiAZ={group.get('MultiAZ')}, AutomaticFailover={group.get('AutomaticFailover')}.",
                 "Add a replica in another AZ and enable Multi-AZ with automatic failover.",
                 "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/AutoFailover.html", severity="MEDIUM", evidence=[call["id"]], target=target)

    # Aurora clusters.
    for cluster in typed("rds.cluster"):
        a = cluster["attributes"]
        if not str(a.get("Engine", "")).startswith("aurora"):
            continue
        members = a.get("DBClusterMembers") or []
        readers = [m for m in members if not m.get("IsClusterWriter")]
        if members:
            emit("AURORA_SINGLE_INSTANCE", "Reliability", cluster, "PASS" if readers else "FAIL",
                 f"{cluster['name']}: {len(members)} instance(s), {len(readers)} reader(s). " +
                 ("" if readers else "With no replica to promote, an instance or AZ failure stops reads and writes until a new instance is created."),
                 "Add an Aurora Replica in another AZ with a failover tier, then run a failover test and record the recovery time.",
                 "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html", weight=4, severity="HIGH")
        if isinstance(a.get("BackupRetentionPeriod"), int):
            days = a["BackupRetentionPeriod"]
            emit("RDS_BACKUPS", "Reliability", cluster, "PASS" if days >= 7 else "WARN",
                 f"{cluster['name']}: automated backup retention {days} day(s); logical errors older than that cannot be undone from backups.",
                 "Set retention to match the recovery point objective (7 to 35 days) and copy long-term backups with AWS Backup.",
                 SH + "rds-controls.html#rds-11", severity="MEDIUM")
        if "DeletionProtection" in a:
            emit("RDS_DELETION_PROTECTION", "Reliability", cluster, "PASS" if a["DeletionProtection"] else "WARN",
                 f"{cluster['name']}: DeletionProtection={a['DeletionProtection']}, BackupRetentionPeriod={a.get('BackupRetentionPeriod')}.",
                 "Enable deletion protection and set the stack DeletionPolicy/UpdateReplacePolicy to Snapshot.",
                 SH + "rds-controls.html#rds-7", severity="MEDIUM")

    # Secrets rotation metadata.
    for call in evidence.records("secretsmanager", "describe-secret"):
        data = evidence.data(call) or {}
        emit("SECRET_ROTATION", "Security", None, "PASS" if data.get("RotationEnabled") else "WARN",
             f"{data.get('Name')}: RotationEnabled={bool(data.get('RotationEnabled'))}, LastRotatedDate={data.get('LastRotatedDate') or 'never'}.",
             "Turn on rotation (or use RDS-managed master passwords) and make clients re-read the secret after rotation.",
             SH + "secretsmanager-controls.html#secretsmanager-1", severity="MEDIUM", evidence=[call["id"]], target=data.get("ARN") or data.get("Name"))

    # Bedrock invocation logging and guardrails (collected only when the workload declares Bedrock use).
    for call in evidence.records("bedrock", "get-model-invocation-logging-configuration"):
        config = (evidence.data(call) or {}).get("loggingConfig")
        emit("GENAI_INVOCATION_LOGGING", "Operational Excellence", None, "PASS" if config else "WARN",
             "Model invocation logging is " + ("configured." if config else "not configured in this account and region; prompts, responses and token use leave no record."),
             "Enable model invocation logging to CloudWatch Logs or S3 with a retention period, and limit who can read it.",
             "https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html", severity="MEDIUM",
             evidence=[call["id"]], target=f"Amazon Bedrock · {call.get('region')}")
    for call in evidence.records("bedrock", "list-guardrails"):
        guardrails = (evidence.data(call) or {}).get("guardrails", [])
        emit("GENAI_GUARDRAILS", "Security", None, "WARN" if not guardrails else "UNKNOWN",
             f"{len(guardrails)} guardrail(s) defined in {call.get('region')}." +
             (" No model call can apply one." if not guardrails else " Whether the application applies one per call needs source or IAM-condition evidence."),
             "Define a guardrail for the use case and enforce it with the bedrock:GuardrailIdentifier IAM condition key.",
             "https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-permissions-id.html", severity="MEDIUM",
             evidence=[call["id"]], target=f"Amazon Bedrock · {call.get('region')}",
             question=None if not guardrails else f"Does the application apply one of the {len(guardrails)} guardrails defined in {call.get('region')} on each model call, and which one?")

    # ECS service autoscaling.
    targets = None
    for call in evidence.records("application-autoscaling", "describe-scalable-targets"):
        targets = (call, {t.get("ResourceId") for t in (evidence.data(call) or {}).get("ScalableTargets", [])})
    if targets:
        call, ids = targets
        for svc in typed("ecs.service"):
            cluster = str(svc["attributes"].get("clusterArn", "")).rsplit("/", 1)[-1]
            resource_id = f"service/{cluster}/{svc['name']}"
            ok = resource_id in ids
            emit("ECS_AUTOSCALING", "Performance Efficiency", svc, "PASS" if ok else "WARN",
                 f"{svc['name']}: desiredCount={svc['attributes'].get('desiredCount')}, " +
                 ("an Application Auto Scaling target exists." if ok else "no Application Auto Scaling target; capacity is fixed."),
                 "Add target tracking on CPU or ALB requests per target with a minimum of two tasks across AZs.",
                 "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html", severity="MEDIUM",
                 evidence=svc["evidence_ids"] + [call["id"]])

    # EKS node groups with fixed size.
    for call in evidence.records("eks", "describe-nodegroup"):
        ng = (evidence.data(call) or {}).get("nodegroup") or {}
        scaling = ng.get("scalingConfig") or {}
        if not scaling:
            continue
        fixed = scaling.get("minSize") == scaling.get("maxSize")
        emit("EKS_NODE_SCALING", "Reliability", None, "WARN" if fixed else "PASS",
             f"{ng.get('nodegroupName')}: min/desired/max={scaling.get('minSize')}/{scaling.get('desiredSize')}/{scaling.get('maxSize')}, "
             f"{', '.join(ng.get('instanceTypes') or [])} {ng.get('capacityType', '')}." + (" No headroom when a node is lost or load grows." if fixed else ""),
             "Raise maxSize and use Karpenter, Cluster Autoscaler or EKS Auto Mode; check pod topology spread and disruption budgets.",
             "https://docs.aws.amazon.com/eks/latest/userguide/autoscaling.html", severity="MEDIUM", evidence=[call["id"]],
             target=ng.get("nodegroupArn") or ng.get("nodegroupName"))

    # AWS Backup coverage for databases in scope.
    plans = evidence.records("backup", "list-backup-plans")
    protected = evidence.records("backup", "list-protected-resources")
    databases = typed("rds.cluster") + typed("rds.instance") + typed("dynamodb.table")
    if plans and protected and databases:
        covered = {r.get("ResourceArn") for r in (evidence.data(protected[0]) or {}).get("Results", [])}
        plan_count = len((evidence.data(plans[0]) or {}).get("BackupPlansList", []))
        for db in databases:
            arn = db["attributes"].get("DBClusterArn") or db["attributes"].get("DBInstanceArn") or db["attributes"].get("TableArn")
            if db["type"] == "rds.instance" and db["attributes"].get("DBClusterIdentifier"):
                continue
            ok = arn in covered
            emit("BACKUP_PLAN", "Reliability", db, "PASS" if ok else "WARN",
                 f"{db['name']}: {'protected by AWS Backup' if ok else 'not in AWS Backup protected resources'} ({plan_count} backup plan(s) in the account). "
                 "Other backup mechanisms outside this API are not visible.",
                 "Define RPO/RTO and protect the database with an AWS Backup plan that keeps copies with their own retention.",
                 "https://docs.aws.amazon.com/aws-backup/latest/devguide/creating-a-backup-plan.html", severity="MEDIUM",
                 evidence=db["evidence_ids"] + [plans[0]["id"], protected[0]["id"]])

    # Delivery pipeline quality gates.
    for call in evidence.records("codepipeline", "get-pipeline"):
        pipeline = (evidence.data(call) or {}).get("pipeline") or {}
        categories = {a.get("actionTypeId", {}).get("category") for st in pipeline.get("stages", []) for a in st.get("actions", [])}
        stages = " → ".join(st.get("name", "?") for st in pipeline.get("stages", []))
        gated = bool(categories & {"Test", "Approval"})
        emit("CI_TEST_GATE", "Operational Excellence", None, "PASS" if gated else "WARN",
             f"Pipeline {pipeline.get('name')}: {stages}; " + ("has a test or approval action." if gated else "no test or approval action before deployment."),
             "Add a test action (unit tests, image scan with a severity threshold) and, for production, a staging stage or manual approval.",
             "https://docs.aws.amazon.com/codepipeline/latest/userguide/approvals.html", severity="MEDIUM", evidence=[call["id"]],
             target=pipeline.get("name"))

    # Processor architecture (Graviton) for compute in scope.
    x86 = []
    for inst in typed("ec2.instance"):
        kind = str(inst["attributes"].get("InstanceType", ""))
        family = kind.split(".")[0]
        if family and not re.search(r"\d+g[a-z]*$", family):
            x86.append(f"{inst['name']} {kind}")
    for call in evidence.records("ecs", "describe-task-definition"):
        td = (evidence.data(call) or {}).get("taskDefinition") or {}
        arch = (td.get("runtimePlatform") or {}).get("cpuArchitecture") or "X86_64"
        if arch != "ARM64":
            x86.append(f"{td.get('family')} Fargate {arch}")
    for call in evidence.records("eks", "describe-nodegroup"):
        ng = (evidence.data(call) or {}).get("nodegroup") or {}
        types = [t for t in ng.get("instanceTypes") or [] if not re.search(r"\d+g[a-z]*$", t.split(".")[0])]
        if types:
            x86.append(f"{ng.get('nodegroupName')} {', '.join(types)}")
    if x86:
        emit("GRAVITON", "Sustainability", None, "WARN",
             f"x86 compute in scope: {'; '.join(x86[:8])}{' …' if len(x86) > 8 else ''}.",
             "Build multi-architecture images and move compatible tasks and nodes to Graviton (ARM64); test native dependencies first.",
             "https://docs.aws.amazon.com/wellarchitected/latest/sustainability-pillar/sus_sus_hardware_a3.html", severity="LOW",
             evidence=sorted({e for r in typed("ec2.instance") for e in r["evidence_ids"]}), target="compute in scope")

    # Alarm coverage: which scoped resources have no CloudWatch alarm at all. An alarm covers a resource when one of its
    # dimensions names the resource (instance id, cluster/service name, DB identifier, load balancer or target group
    # suffix, cache cluster id, function name). Only resource types people routinely alarm on are judged.
    alarms = typed("cloudwatch.alarm") or [r for r in resources if r["type"] == "cloudwatch.alarm"]
    dimensions = set()  # (namespace, dimension name, value)
    for alarm in alarms:
        a = alarm["attributes"]
        metrics = [(a.get("Namespace"), a.get("Dimensions") or [])] + [
            (((m.get("MetricStat") or {}).get("Metric") or {}).get("Namespace"), ((m.get("MetricStat") or {}).get("Metric") or {}).get("Dimensions") or [])
            for m in a.get("Metrics", []) or []]
        for namespace, dims in metrics:
            for d in dims:
                dimensions.add((namespace, d.get("Name"), str(d.get("Value"))))
    alarm_evidence = sorted({e for alarm in alarms for e in alarm["evidence_ids"]})

    def covered(pairs):
        return any((ns, name, str(value)) in dimensions for ns, name, value in pairs if value)
    expected = {
        "elbv2.load-balancer": ("ALB 5XX, target 5XX, latency and UnHealthyHostCount",
                                lambda a: [("AWS/ApplicationELB", "LoadBalancer", a.get("LoadBalancerArn", "").split(":loadbalancer/")[-1]),
                                           ("AWS/NetworkELB", "LoadBalancer", a.get("LoadBalancerArn", "").split(":loadbalancer/")[-1])]),
        "rds.cluster": ("CPU, connections, freeable memory, replica lag", lambda a: [("AWS/RDS", "DBClusterIdentifier", a.get("DBClusterIdentifier"))]),
        "rds.instance": ("CPU, connections, freeable memory", lambda a: [("AWS/RDS", "DBInstanceIdentifier", a.get("DBInstanceIdentifier"))]),
        "ecs.service": ("CPU/memory utilisation and running task count", lambda a: [("AWS/ECS", "ServiceName", a.get("serviceName")), ("ECS/ContainerInsights", "ServiceName", a.get("serviceName"))]),
        "eks.cluster": ("node readiness, pod restarts (Container Insights)", lambda a: [("ContainerInsights", "ClusterName", a.get("name"))]),
        "ec2.instance": ("StatusCheckFailed, CPU, memory and disk (agent)", lambda a: [("AWS/EC2", "InstanceId", a.get("InstanceId")), ("CWAgent", "InstanceId", a.get("InstanceId"))]),
        "lambda.function": ("errors, throttles, duration", lambda a: [("AWS/Lambda", "FunctionName", a.get("FunctionName"))]),
        "dynamodb.table": ("throttled requests, system errors", lambda a: [("AWS/DynamoDB", "TableName", a.get("TableName"))]),
    }
    for kind, (what, keys) in expected.items():
        for r in typed(kind):
            if kind == "rds.instance" and covered([("AWS/RDS", "DBClusterIdentifier", r["attributes"].get("DBClusterIdentifier"))]):
                continue
            if kind == "ec2.instance" and any(t.get("Key") == "aws:eks:cluster-name" for t in r["attributes"].get("Tags") or []):
                continue  # EKS nodes are judged at cluster level
            ok = covered(keys(r["attributes"]))
            emit("ALARM_COVERAGE", "Operational Excellence", r, "PASS" if ok else "WARN",
                 f"{r['name']}: {'at least one alarm references this resource' if ok else 'no CloudWatch alarm references this resource'}; expected: {what}.",
                 "Create alarms on the metrics that show user impact first (errors, latency, availability), then saturation; route them to the subscribed topic.",
                 "https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/ops_workload_observability_create_alerts.html",
                 severity="MEDIUM", evidence=r["evidence_ids"] + alarm_evidence)

    # Log delivery: a log group that stopped receiving events, or never received any, while its producer exists.
    now_ms = time.time() * 1000
    producers = {}
    for call in evidence.records("ecs", "describe-task-definition"):
        td = (evidence.data(call) or {}).get("taskDefinition") or {}
        for c in td.get("containerDefinitions", []) or []:
            opts = ((c.get("logConfiguration") or {}).get("options") or {})
            if opts.get("awslogs-group"):
                producers[opts["awslogs-group"]] = f"ECS task definition {td.get('family')}"
    for call in evidence.records("logs", "describe-log-streams"):
        group = evidence.arg(call, "--log-group-name")
        streams = (evidence.data(call) or {}).get("logStreams") or []
        resource = next((r for r in resources if r["type"] == "logs.group" and r["native_id"] == group), None)
        last = max((x.get("lastEventTimestamp") or 0 for x in streams), default=0)
        age_h = (now_ms - last) / 3_600_000 if last else None
        producer = producers.get(group)
        limit_h = 72 if producer else 14 * 24  # a known always-on producer should write daily; otherwise allow bursty use
        if last == 0:
            status, text = ("WARN" if producer else "UNKNOWN"), "has no log events" + (f" although {producer} writes to it" if producer else "; no producer identified")
        elif age_h > limit_h:
            status, text = "WARN", f"last event {age_h / 24:.0f} days ago" + (f"; {producer} still references it" if producer else "")
        else:
            status, text = "PASS", f"last event {age_h / 24:.1f} day(s) ago" if age_h >= 48 else f"last event {age_h:.0f} h ago"
        emit("LOG_DELIVERY", "Operational Excellence", resource, status, f"{group}: {text}.",
             "Check the log driver or agent configuration of the producer and the IAM permission to write; a silent log group hides failures.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html", severity="MEDIUM",
             evidence=(resource["evidence_ids"] if resource else []) + [call["id"]], target=None if resource else group,
             question=None if status != "UNKNOWN" else f"Does anything still write to log group {group}, or can it be removed?")

    # CloudWatch agent on EC2: memory and disk metrics exist only with the agent (namespace CWAgent).
    agent_calls = evidence.records("cloudwatch", "list-metrics")
    if agent_calls:
        reporting = {i for call in agent_calls for m in (evidence.data(call) or {}).get("Metrics", []) for i in m.get("InstanceIds", [])}
        for inst in typed("ec2.instance"):
            if any(t.get("Key") == "aws:eks:cluster-name" for t in inst["attributes"].get("Tags") or []):
                continue
            ok = inst["native_id"] in reporting
            emit("EC2_CLOUDWATCH_AGENT", "Operational Excellence", inst, "PASS" if ok else "WARN",
                 f"{inst['name']}: {'reports CWAgent metrics' if ok else 'no CWAgent metrics; memory, disk and process metrics are not collected'}.",
                 "Install the CloudWatch agent through SSM (AmazonCloudWatch-ManageAgent) with a configuration that includes memory and disk, or run the host as a container with Container Insights.",
                 "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Install-CloudWatch-Agent.html", severity="LOW",
                 evidence=inst["evidence_ids"] + [c["id"] for c in agent_calls])

    # EKS observability add-on (Container Insights for EKS).
    for call in evidence.records("eks", "list-addons"):
        cluster_name = evidence.arg(call, "--cluster-name")
        eks = next((r for r in typed("eks.cluster") if (r["attributes"].get("name") or r["native_id"]) == cluster_name), None)
        addons = (evidence.data(call) or {}).get("addons") or []
        ok = "amazon-cloudwatch-observability" in addons
        emit("EKS_OBSERVABILITY_ADDON", "Operational Excellence", eks, "PASS" if ok else "WARN",
             f"{cluster_name}: add-ons {', '.join(addons) or 'none'}; {'Container Insights add-on present' if ok else 'no amazon-cloudwatch-observability add-on, so no pod/node metrics or application logs in CloudWatch'}.",
             "Install the amazon-cloudwatch-observability add-on (Container Insights with enhanced observability, Application Signals) or an equivalent collector.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/install-CloudWatch-Observability-EKS-addon.html", severity="MEDIUM",
             evidence=(eks["evidence_ids"] if eks else []) + [call["id"]], target=None if eks else cluster_name)
