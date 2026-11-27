"""Baseline checks that read the supplementary workload evidence (supplement.py) as well as the inventory.

Each rule is deterministic, judges only what the recorded API responses show, and names its target. They
cover the gaps that agents previously filled with ad-hoc scripts: listener TLS on every load balancer in
scope (including ones created outside CloudFormation), IAM wildcard permissions in workload role policies,
alarm notification targets without subscribers, ECR/ECS/EKS/ElastiCache/Aurora/Secrets Manager settings and
Bedrock invocation logging and guardrails. Agents may override a status or severity in checks.manual.json
with an assessment_reason; they should not need to re-author these checks.

Since moda-starter-1.3 they also judge what agents used to write by hand from the same records: single EC2 instances
outside an Auto Scaling group, mutable image tags in ECS task definitions, security groups that open data ports to
CIDR ranges, IAM users in EKS access entries, EKS secrets encryption, database diagnostics, dashboard widgets that
name resources that do not exist, iam:PassRole on any role, UserData that pipes a download into a shell, and (for a
non-production scope) NAT gateways per AZ and provisioned Aurora capacity. Account-wide records (describe-repositories,
describe-replication-groups and similar) skip items that are inventory resources outside the scope.
"""
from fnmatch import fnmatchcase
import ipaddress
import json
import re
import time
from urllib.parse import unquote

SH = "https://docs.aws.amazon.com/securityhub/latest/userguide/"
# Ports of databases, caches and data stores that clients should reach through a security-group reference.
DATA_PORTS = {3306: "MySQL", 5432: "PostgreSQL", 1433: "SQL Server", 1521: "Oracle", 6379: "Redis", 11211: "Memcached",
              27017: "MongoDB", 9042: "Cassandra", 5439: "Redshift", 8182: "Neptune"}
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
# Dashboard metric dimensions that name one resource, by namespace: (inventory types, value of a resource).
DASHBOARD_DIMENSIONS = {
    ("AWS/ApplicationELB", "LoadBalancer"): ("elbv2", ("elbv2.load-balancer",), lambda r: str(r["native_id"]).split(":loadbalancer/")[-1]),
    ("AWS/NetworkELB", "LoadBalancer"): ("elbv2", ("elbv2.load-balancer",), lambda r: str(r["native_id"]).split(":loadbalancer/")[-1]),
    ("AWS/ApplicationELB", "TargetGroup"): ("elbv2", ("elbv2.target-group",), lambda r: str(r["native_id"]).rsplit(":", 1)[-1]),
    ("AWS/NetworkELB", "TargetGroup"): ("elbv2", ("elbv2.target-group",), lambda r: str(r["native_id"]).rsplit(":", 1)[-1]),
    ("AWS/EC2", "InstanceId"): ("ec2", ("ec2.instance",), lambda r: r["native_id"]),
    ("CWAgent", "InstanceId"): ("ec2", ("ec2.instance",), lambda r: r["native_id"]),
    ("AWS/RDS", "DBClusterIdentifier"): ("rds", ("rds.cluster",), lambda r: r["attributes"].get("DBClusterIdentifier") or r["native_id"]),
    ("AWS/RDS", "DBInstanceIdentifier"): ("rds", ("rds.instance",), lambda r: r["attributes"].get("DBInstanceIdentifier") or r["native_id"]),
    ("AWS/ECS", "ClusterName"): ("ecs", ("ecs.cluster",), lambda r: r["attributes"].get("clusterName") or r["name"]),
    ("ECS/ContainerInsights", "ClusterName"): ("ecs", ("ecs.cluster",), lambda r: r["attributes"].get("clusterName") or r["name"]),
    ("AWS/ECS", "ServiceName"): ("ecs", ("ecs.service",), lambda r: r["attributes"].get("serviceName") or r["name"]),
    ("ECS/ContainerInsights", "ServiceName"): ("ecs", ("ecs.service",), lambda r: r["attributes"].get("serviceName") or r["name"]),
    ("ContainerInsights", "ClusterName"): ("eks", ("eks.cluster",), lambda r: r["attributes"].get("name") or r["native_id"]),
    ("AWS/ElastiCache", "CacheClusterId"): ("elasticache", ("elasticache.cluster",), lambda r: r["native_id"]),
    ("AWS/Lambda", "FunctionName"): ("lambda", ("lambda.function",), lambda r: r["attributes"].get("FunctionName") or r["name"]),
    ("AWS/DynamoDB", "TableName"): ("dynamodb", ("dynamodb.table",), lambda r: r["attributes"].get("TableName") or r["name"]),
}
# Download URLs that point at a moving reference rather than a release (branch names used by upstream repositories).
# A download URL pinned to a commit or a release: a 40-character commit id, or a version such as v3.15.2 / 1.35.0.
PINNED_REF = re.compile(r"/(?:[0-9a-f]{40}|v?\d+(?:\.\d+)+(?:[-+][\w.]+)?)(?:/|$)")


def moving_ref(url):
    """True when the URL names no fixed commit or version (a branch such as main, or 'latest')."""
    return "latest" in url.lower() or not PINNED_REF.search(url)
PIPE_TO_SHELL = (
    re.compile(r"\b(?:curl|wget)\b[^|\n]*?(?P<url>https?://[^\s\"'|)]+)[^|\n]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
               r"(?:/usr/bin/env\s+)?(?:/usr/bin/|/bin/)?(?P<shell>bash|sh|zsh)\b"),
    re.compile(r"\b(?P<shell>bash|sh|zsh)\s+(?:-c\s+)?[\"']?(?:<\(|\$\()\s*(?:curl|wget)\b[^)\n]*?(?P<url>https?://[^\s\"'|)]+)"),
)


def tag_map(attributes):
    return {t.get("Key"): t.get("Value") for t in attributes.get("Tags") or [] if isinstance(t, dict)}


def is_eks_node(attributes):
    tags = tag_map(attributes)
    return any(k in tags for k in ("aws:eks:cluster-name", "eks:cluster-name", "karpenter.sh/nodepool")) or any(
        str(k).startswith("kubernetes.io/cluster/") for k in tags)


def is_admin_host(attributes):
    tags = tag_map(attributes)
    return bool(re.search(r"bastion|jump|admin", f"{tags.get('Name', '')} {tags.get('Role', '')}", re.I))


def fleet_managed(attributes):
    """Instances that EMR or an EC2/Spot Fleet launches and may replace; their recovery is not the instance's own."""
    tags = tag_map(attributes)
    return any(k in tags for k in ("aws:elasticmapreduce:job-flow-id", "aws:ec2spot:fleet-request-id", "aws:ec2:fleet-id"))


def running(instance):
    return ((instance["attributes"].get("State") or {}).get("Name")) == "running"


def instance_label(instance):
    name = tag_map(instance["attributes"]).get("Name")
    return f"{instance['native_id']} ({name})" if name and name != instance["native_id"] else instance["native_id"]


def image_reference(image):
    """(repository path, tag, digest) of a container image reference; the registry host is dropped."""
    image = str(image or "")
    digest = image.split("@", 1)[1] if "@" in image else None
    name = image.split("@", 1)[0]
    tag = None
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    parts = name.split("/", 1)
    path = parts[1] if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost") else name
    return path, tag, digest


def is_private(cidr):
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    if net.prefixlen == 0:
        return False
    return net.is_private or (net.version == 4 and net.subnet_of(SHARED_ADDRESS_SPACE))


def expand_metrics(rows):
    """CloudWatch dashboard metric rows with the "." and "..." shorthands resolved; expression rows are skipped."""
    out, previous = [], []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list) or not row or isinstance(row[0], dict):
            continue
        items = [x for x in row if not isinstance(x, dict)]
        if "..." in items:
            i = items.index("...")
            head, tail = items[:i], items[i + 1:]
            middle = previous[len(head):len(previous) - len(tail)] if len(previous) >= len(head) + len(tail) else []
            items = head + middle + tail
        items = [previous[k] if x == "." and k < len(previous) else x for k, x in enumerate(items)]
        previous = items
        out.append(items)
    return out


def allows(action_patterns, action):
    """True when one of the statement's action patterns matches the action (IAM matching is case-insensitive)."""
    return any(fnmatchcase(action.lower(), str(p).lower()) for p in action_patterns)


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


def evaluate_workload(emit, resources, evidence, scoped, outside=None, context=None):
    """emit(rule, pillar, resource, status, observation, recommendation, url, weight, severity, evidence, target).

    `outside` holds (type, native_id) pairs of inventory resources outside the scope: an item of an account-wide record
    that is one of them is skipped, while an item that is no inventory resource at all is judged as before. `context`
    carries the run scope's environment, the full inventory (for references to resources outside the scope) and the
    manifest truncations."""
    outside = outside or set()
    context = context or {}
    environment = context.get("environment")
    typed = lambda kind: [r for r in resources if r["type"] == kind and r["id"] in scoped]  # noqa: E731
    by_native = {}
    for r in resources:
        by_native.setdefault(r["native_id"], r)

    def excluded(kind, *values):
        return any((kind, v) in outside for v in values if v)

    def record_outside(call):
        """A workload record collected for resources that are all outside the scope."""
        parts = [str(i).split(":", 3) for i in call.get("resource_ids") or []]
        return bool(parts) and all(len(p) == 4 and (p[2], p[3]) in outside for p in parts)

    task_definitions = [(c, (evidence.data(c) or {}).get("taskDefinition") or {})
                        for c in evidence.records("ecs", "describe-task-definition") if not record_outside(c)]
    user_data = {}  # instance id -> (call, decoded UserData) for in-scope instances
    instances_by_id = {r["native_id"]: r for r in typed("ec2.instance")}
    for call in evidence.records("ec2", "describe-instance-attribute"):
        data = evidence.data(call) or {}
        decoded = (data.get("UserData") or {}).get("Decoded") if isinstance(data.get("UserData"), dict) else None
        inst = instances_by_id.get(data.get("InstanceId") or evidence.arg(call, "--instance-id"))
        if decoded and inst:
            user_data[inst["native_id"]] = (call, str(decoded))
    profile_roles = {}  # instance profile ARN -> (call, role names)
    for call in evidence.records("iam", "get-instance-profile"):
        profile = (evidence.data(call) or {}).get("InstanceProfile") or {}
        if profile.get("Arn"):
            profile_roles[profile["Arn"]] = (call, [r.get("RoleName") for r in profile.get("Roles") or [] if r.get("RoleName")])

    def role_users(role_name):
        """Workload resources that run with this role: ECS services through their task role, EC2 instances through
        their instance profile. Returns (descriptions, evidence ids)."""
        names, ids = [], []
        for call, td in task_definitions:
            if str(td.get("taskRoleArn") or "").rsplit("/", 1)[-1] != role_name:
                continue
            services = [s for s in typed("ecs.service") if s["attributes"].get("taskDefinition") == td.get("taskDefinitionArn")]
            names += [f"ECS service {s['name']} (task role)" for s in services] or [f"ECS task definition {td.get('family')}:{td.get('revision')} (task role)"]
            ids.append(call["id"])
        for inst in typed("ec2.instance"):
            arn = (inst["attributes"].get("IamInstanceProfile") or {}).get("Arn")
            call, roles = profile_roles.get(arn, (None, []))
            if role_name in roles:
                names.append(f"EC2 instance {instance_label(inst)} through instance profile {arn.rsplit('/', 1)[-1]}")
                ids.append(call["id"])
        return list(dict.fromkeys(names)), ids

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
    docs = [(c, d) for c, d in docs if not record_outside(c)]
    for call, document in docs:
        role = call.get("provenance", {}).get("origin") or evidence.arg(call, "--role-name")
        role_name = str(evidence.arg(call, "--role-name") or role or "").rsplit("/", 1)[-1]
        broad, scoped_wild, pass_any = [], [], []
        for st in statements(document):
            if st.get("Effect") != "Allow":
                continue
            actions = [str(x) for x in as_list(st.get("Action"))]
            targets = [str(x) for x in as_list(st.get("Resource"))]
            # iam:PassRole on any role. "*" and "iam:*" are reported by IAM_WILDCARD_PERMISSION below.
            if "*" in targets and allows([a for a in actions if a not in ("*", "iam:*")], "iam:PassRole"):
                passed = as_list(((st.get("Condition") or {}).get("StringEquals") or {}).get("iam:PassedToService")) + \
                    as_list(((st.get("Condition") or {}).get("StringLike") or {}).get("iam:PassedToService"))
                pass_any.append(", ".join(str(p) for p in passed))
            wild = [x for x in actions if x == "*" or x.endswith(":*")]
            if not wild:
                continue
            (broad if "*" in targets else scoped_wild).append(f"{', '.join(wild)} on {', '.join(targets[:3])}")
        resource = next((r for r in resources if r["id"] in call.get("resource_ids", [])), None)
        name = evidence.arg(call, "--policy-name") or evidence.arg(call, "--policy-arn") or "policy"
        users, user_evidence = role_users(role_name) if (broad or scoped_wild or pass_any) else ([], [])
        used_by = f" Used by {', '.join(users[:6])}{' and others' if len(users) > 6 else ''}." if users else ""
        if pass_any:
            services = sorted({s for p in pass_any for s in p.split(", ") if s})
            unconditioned = any(not p for p in pass_any)
            to = "any service" if unconditioned or not services else ", ".join(services)
            emit("IAM_PASSROLE_ANY", "Security", resource, "WARN",
                 f"Role {role} policy {name} allows iam:PassRole on Resource \"*\"" +
                 (f" with iam:PassedToService {', '.join(services)}" if services and not unconditioned else " without an iam:PassedToService condition") +
                 f"; whoever acts with this role can hand any role in the account, including more privileged ones, to {to}.{used_by}",
                 "Consider listing the role ARNs it needs to pass (for example the task and execution roles of its own services) in Resource and keeping "
                 "an iam:PassedToService condition; that closes the path to more privileged roles. Resource \"*\" is hard to justify outside a deployment "
                 "tool that creates the roles it passes.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_passrole.html",
                 # a service condition narrows who can receive the role, so the finding is a refinement rather than an open path
                 weight=3 if unconditioned or not services else 2, severity="MEDIUM" if unconditioned or not services else "LOW",
                 evidence=[call["id"]] + user_evidence, target=f"{role} · {name}")
        if not (broad or scoped_wild):
            continue
        emit("IAM_WILDCARD_PERMISSION", "Security", resource,
             "FAIL" if broad else "WARN",
             f"Role {role} policy {name} allows {'; '.join(broad + scoped_wild)}.{used_by}",
             "Replace service-wide wildcards with the actions the workload calls and the specific resource ARNs; generate a starting policy from CloudTrail with IAM Access Analyzer.",
             "https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html#grant-least-privilege",
             weight=4 if broad else 2, severity="HIGH" if broad else "MEDIUM", evidence=[call["id"]] + user_evidence, target=f"{role} · {name}")

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
            if excluded("ecr.repository", repo.get("repositoryArn"), repo.get("repositoryName")):
                continue  # an account-wide listing also returns repositories of other workloads
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
            if excluded("elasticache.replication-group", group.get("ReplicationGroupId"), group.get("ARN")):
                continue
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
        if record_outside(call) or excluded("secretsmanager.secret", data.get("ARN"), data.get("Name")):
            continue
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
        if record_outside(call) or excluded("eks.cluster", ng.get("clusterName")):
            continue
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
        if record_outside(call) or excluded("codepipeline.pipeline", pipeline.get("name")):
            continue
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
    for call, td in task_definitions:
        arch = (td.get("runtimePlatform") or {}).get("cpuArchitecture") or "X86_64"
        if arch != "ARM64":
            x86.append(f"{td.get('family')} Fargate {arch}")
    for call in evidence.records("eks", "describe-nodegroup"):
        ng = (evidence.data(call) or {}).get("nodegroup") or {}
        if record_outside(call) or excluded("eks.cluster", ng.get("clusterName")):
            continue
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
    producers = {}  # log group -> (producer, clause after "although", evidence ids)
    for call, td in task_definitions:
        for c in td.get("containerDefinitions", []) or []:
            opts = ((c.get("logConfiguration") or {}).get("options") or {})
            if opts.get("awslogs-group"):
                label = f"ECS task definition {td.get('family')}"
                producers[opts["awslogs-group"]] = (label, f"{label} writes to it", [call["id"]])
    role_writers = {}  # role name -> [(log group ARN pattern, call)]
    for call, document in docs:
        role_name = str(evidence.arg(call, "--role-name") or call.get("provenance", {}).get("origin") or "").rsplit("/", 1)[-1]
        for st in statements(document):
            actions = [str(a) for a in as_list(st.get("Action")) if str(a).lower().startswith("logs:")]
            if st.get("Effect") == "Allow" and allows(actions, "logs:PutLogEvents"):
                role_writers.setdefault(role_name, []).extend((str(t), call) for t in as_list(st.get("Resource")) if ":log-group:" in str(t))

    def ec2_producer(group):
        """An in-scope running EC2 instance whose UserData names the group, or whose instance role may write to it."""
        pattern = re.compile(rf"(?<![\w/.-]){re.escape(group)}(?![\w/.-])")
        instances = [i for i in typed("ec2.instance") if running(i)]
        for inst in instances:
            call, decoded = user_data.get(inst["native_id"], (None, ""))
            if call and pattern.search(decoded):
                label = f"EC2 instance {inst['native_id']} (its UserData configures this log group)"
                return label, f"{label} is running", [call["id"]]
        arn_pattern = re.compile(rf":log-group:{re.escape(group)}(?::|$)")
        for inst in instances:
            profile_call, roles = profile_roles.get((inst["attributes"].get("IamInstanceProfile") or {}).get("Arn"), (None, []))
            for role_name in roles:
                hit = next((c for arn, c in role_writers.get(role_name, []) if arn_pattern.search(arn)), None)
                if hit:
                    label = f"EC2 instance {inst['native_id']} (its instance role may write to this log group)"
                    return label, f"{label} is running", [profile_call["id"], hit["id"]]
        return None

    for call in evidence.records("logs", "describe-log-streams"):
        group = evidence.arg(call, "--log-group-name")
        if excluded("logs.group", group):
            continue
        streams = (evidence.data(call) or {}).get("logStreams") or []
        resource = next((r for r in resources if r["type"] == "logs.group" and r["native_id"] == group), None)
        last = max((x.get("lastEventTimestamp") or 0 for x in streams), default=0)
        age_h = (now_ms - last) / 3_600_000 if last else None
        producer, clause, producer_evidence = producers.get(group) or (group and ec2_producer(group)) or (None, None, [])
        limit_h = 72 if producer else 14 * 24  # a known always-on producer should write daily; otherwise allow bursty use
        if last == 0:
            status, text = ("WARN" if producer else "UNKNOWN"), "has no log events" + (f" although {clause}" if producer else "; no producer identified")
        elif age_h > limit_h:
            status, text = "WARN", f"last event {age_h / 24:.0f} days ago" + (f"; {producer} still references it" if producer else "")
        else:
            status, text = "PASS", f"last event {age_h / 24:.1f} day(s) ago" if age_h >= 48 else f"last event {age_h:.0f} h ago"
        emit("LOG_DELIVERY", "Operational Excellence", resource, status, f"{group}: {text}.",
             "Check the log driver or agent configuration of the producer and the IAM permission to write; a silent log group hides failures.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html", severity="MEDIUM",
             evidence=(resource["evidence_ids"] if resource else []) + [call["id"]] + (producer_evidence if status != "PASS" else []),
             target=None if resource else group,
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
        if excluded("eks.cluster", cluster_name):
            continue
        eks = next((r for r in typed("eks.cluster") if (r["attributes"].get("name") or r["native_id"]) == cluster_name), None)
        addons = (evidence.data(call) or {}).get("addons") or []
        ok = "amazon-cloudwatch-observability" in addons
        emit("EKS_OBSERVABILITY_ADDON", "Operational Excellence", eks, "PASS" if ok else "WARN",
             f"{cluster_name}: add-ons {', '.join(addons) or 'none'}; {'Container Insights add-on present' if ok else 'no amazon-cloudwatch-observability add-on, so no pod/node metrics or application logs in CloudWatch'}.",
             "Install the amazon-cloudwatch-observability add-on (Container Insights with enhanced observability, Application Signals) or an equivalent collector.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/install-CloudWatch-Observability-EKS-addon.html", severity="MEDIUM",
             evidence=(eks["evidence_ids"] if eks else []) + [call["id"]], target=None if eks else cluster_name)

    # ---- rules that agents used to write by hand (moda-starter-1.3) ----------------------------------------------
    # Single EC2 instances outside an Auto Scaling group.
    for inst in [i for i in typed("ec2.instance") if running(i) and not is_eks_node(i["attributes"])]:
        a = inst["attributes"]
        tags = tag_map(a)
        if fleet_managed(a) and not tags.get("aws:autoscaling:groupName"):
            continue
        az = (a.get("Placement") or {}).get("AvailabilityZone") or "one Availability Zone"
        url = "https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-benefits.html"
        if tags.get("aws:autoscaling:groupName"):
            emit("EC2_SINGLE_INSTANCE_NO_ASG", "Reliability", inst, "PASS",
                 f"{instance_label(inst)} runs in Auto Scaling group {tags['aws:autoscaling:groupName']}, which replaces it when it fails a health check.",
                 "Keep the group's minimum and health checks aligned with what the instance serves.", url, severity="MEDIUM")
        elif is_admin_host(a):
            emit("EC2_SINGLE_INSTANCE_NO_ASG", "Reliability", inst, "WARN",
                 f"{instance_label(inst)} is an administration host (Name/Role tag) that runs outside any Auto Scaling group in {az}; "
                 "if it stops, administration through it stops until it is replaced.",
                 "An administration host can usually stay a single instance because it serves operators, not users. If it has to survive an instance "
                 "or AZ failure, an Auto Scaling group of one with a launch template brings it back by itself.", url, severity="LOW")
        else:
            emit("EC2_SINGLE_INSTANCE_NO_ASG", "Reliability", inst, "WARN",
                 f"{instance_label(inst)} runs {a.get('InstanceType', 'an instance')} outside any Auto Scaling group in {az}; "
                 "if it stops, what it serves stops until it is replaced.",
                 "Consider a launch template with an Auto Scaling group across two AZs (it replaces a failed instance without anyone acting), or moving "
                 "the service to a container on an existing cluster. A single instance is a reasonable choice when an outage until a scripted rebuild is acceptable.",
                 url, severity="MEDIUM")

    # Image tags that do not identify one build.
    for call, td in task_definitions:
        images = [(c.get("name"), c.get("image")) for c in td.get("containerDefinitions") or [] if c.get("image")]
        if not images:
            continue
        loose, pinned = [], []
        for container, image in images:
            path, tag, digest = image_reference(image)
            if not digest and (tag is None or tag == "latest"):
                loose.append(f"{container} runs {path}:{tag or '(no tag)'}")
            else:
                pinned.append(f"{path}@{digest}" if digest else f"{path}:{tag}")
        services = [s for s in typed("ecs.service") if s["attributes"].get("taskDefinition") == td.get("taskDefinitionArn")]
        label = f"Task definition {td.get('family')}:{td.get('revision')}"
        for svc in services or [None]:
            emit("IMAGE_TAG_TRACEABILITY", "Operational Excellence", svc, "WARN" if loose else "PASS",
                 (f"{label}: {'; '.join(loose)}. A tag such as latest does not name one build, so which source revision runs cannot be read from the "
                  "configuration, and a re-push changes what the next task start pulls." if loose else
                  f"{label}: every container image is pinned to a version tag or digest ({', '.join(pinned[:4])})."),
                 "Consider tagging images with the commit ID (or deploying by digest) and referencing that tag in the task definition, so each deployment "
                 "names exactly one build and a rollback can return to it; latest is fine for throwaway experiments that are never rolled back.",
                 "https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html", severity="MEDIUM",
                 evidence=(svc["evidence_ids"] if svc else []) + [call["id"]], target=None if svc else f"ECS task definition {td.get('family')}:{td.get('revision')}")

    # Data ports opened to CIDR ranges instead of security-group references.
    for sg in typed("ec2.security-group"):
        a = sg["attributes"]
        if not isinstance(a.get("IpPermissions"), list):
            continue
        hits, public = [], False
        for rule in a["IpPermissions"]:
            if str(rule.get("IpProtocol")) not in ("tcp", "6"):
                continue
            low, high = rule.get("FromPort"), rule.get("ToPort")
            if not (isinstance(low, int) and isinstance(high, int)):
                continue
            ports = [p for p in sorted(DATA_PORTS) if low <= p <= high]
            if not ports:
                continue
            port_text = (f"tcp {low} ({DATA_PORTS[low]})" if low == high else
                         f"tcp {low}-{high} (includes data ports {', '.join(str(p) for p in ports[:4])}{' and more' if len(ports) > 4 else ''})")
            cidrs = [x.get("CidrIp") for x in rule.get("IpRanges") or [] if x.get("CidrIp")] + \
                [x.get("CidrIpv6") for x in rule.get("Ipv6Ranges") or [] if x.get("CidrIpv6")]
            for cidr in cidrs:
                private = is_private(cidr)
                if private is None:
                    continue
                public = public or not private
                hits.append(f"{port_text} from {cidr}")
        name = tag_map(a).get("Name") or a.get("GroupName") or sg["name"]
        url = "https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html"
        if hits:
            emit("SG_CIDR_INSTEAD_OF_REFERENCE", "Security", sg, "FAIL" if public else "WARN",
                 f"Security group {name} ({a.get('GroupId') or sg['native_id']}) admits {'; '.join(hits[:6])} by address range instead of a security-group "
                 "reference; every host in that range can reach the port, not only the clients that need it." +
                 (" A public range makes the port reachable from the internet wherever the network path allows it." if public else ""),
                 ("Remove the public range first, then allow the security groups of the clients that need the port. " if public else "") +
                 "Consider replacing the CIDR rule with references to the clients' security groups (for example the application's task or instance group), "
                 "so other hosts in the same subnets cannot connect. Keeping a CIDR rule is reasonable for clients outside AWS, such as an on-premises network over VPN.",
                 url, weight=3, severity="HIGH" if public else "MEDIUM")

    # EKS access entries and secrets encryption.
    for call in evidence.records("eks", "list-access-entries"):
        cluster_name = evidence.arg(call, "--cluster-name")
        if excluded("eks.cluster", cluster_name) or record_outside(call):
            continue
        eks = next((r for r in typed("eks.cluster") if (r["attributes"].get("name") or r["native_id"]) == cluster_name), None)
        entries = [str(e) for e in (evidence.data(call) or {}).get("accessEntries") or []]
        users = [e for e in entries if ":user/" in e]
        emit("EKS_IAM_USER_ACCESS", "Security", eks, "WARN" if users else "PASS",
             (f"{cluster_name}: access entries include IAM user(s) {', '.join(users[:4])}; an IAM user signs in with long-term credentials." if users else
              f"{cluster_name}: none of the {len(entries)} access entries is an IAM user."),
             "Consider giving people cluster access through an assumed role (an IAM Identity Center permission set, or a role that requires MFA) mapped to a "
             "scoped access policy, then removing the user's entry; a user entry kept only as a documented break-glass path is a reasonable exception.",
             "https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html", severity="LOW",
             evidence=(eks["evidence_ids"] if eks else []) + [call["id"]], target=None if eks else cluster_name)
    for eks in typed("eks.cluster"):
        a = eks["attributes"]
        if "resourcesVpcConfig" not in a and "status" not in a:
            continue  # cluster details were not read
        encrypted = [c for c in a.get("encryptionConfig") or [] if "secrets" in (c.get("resources") or [])]
        emit("EKS_SECRETS_ENCRYPTION", "Security", eks, "PASS" if encrypted else "WARN",
             (f"{eks['name']}: Kubernetes Secrets are envelope-encrypted with KMS key "
              f"{((encrypted[0].get('provider') or {}).get('keyArn') or 'configured').rsplit('/', 1)[-1]}." if encrypted else
              f"{eks['name']}: no encryptionConfig, so Kubernetes Secrets are not encrypted with a KMS key the account controls (Security Hub EKS.3). "
              "EKS encrypts the data at rest by default; a customer managed key adds control over the key policy and a CloudTrail record of each use."),
             "Consider enabling secrets encryption with a customer managed KMS key when the cluster stores credentials as Kubernetes Secrets or a standard asks "
             "for customer managed keys; it cannot be turned off once enabled. When workloads read credentials through Pod Identity or the Secrets Store CSI "
             "driver instead, the gain is small.", SH + "eks-controls.html#eks-3", weight=1, severity="LOW")

    # Database diagnostics.
    clusters = {r["attributes"].get("DBClusterIdentifier") or r["native_id"]: r for r in typed("rds.cluster")}
    for db in typed("rds.instance"):
        a = db["attributes"]
        engine = str(a.get("Engine") or "")
        if "PerformanceInsightsEnabled" not in a or not engine.startswith(("aurora", "postgres", "mysql", "mariadb", "oracle", "sqlserver")):
            continue
        cluster = clusters.get(a.get("DBClusterIdentifier"))
        exports = a.get("EnabledCloudwatchLogsExports") or ((cluster or {}).get("attributes") or {}).get("EnabledCloudwatchLogsExports") or []
        on = bool(a.get("PerformanceInsightsEnabled"))
        emit("DB_DIAGNOSTICS", "Performance Efficiency", db, "PASS" if on else "WARN",
             f"{db['native_id']} ({engine}): PerformanceInsightsEnabled={on}, MonitoringInterval={a.get('MonitoringInterval', 'unknown')}, "
             f"EnabledCloudwatchLogsExports={', '.join(exports) or 'none'}." + ("" if on else " Slow queries and wait events leave no record to analyse."),
             "Consider turning on Performance Insights (the 7-day retention tier has no extra charge) and exporting the engine log to CloudWatch Logs; "
             "Enhanced Monitoring (MonitoringInterval) adds operating-system metrics, billed as CloudWatch Logs usage. For a database nobody tunes, the "
             "default CloudWatch metrics may be enough.",
             "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.Overview.html", severity="LOW",
             evidence=db["evidence_ids"] + ((cluster or {}).get("evidence_ids") or []))

    # Dashboard widgets whose dimensions name resources that do not exist.
    inventory = context.get("inventory") or {}
    every = inventory.get("resources") or resources
    collected, covered_regions = set(inventory.get("services") or []), set(inventory.get("regions") or [])
    truncated = {str(t.get("scope", "")) for t in context.get("truncations") or []}

    def listing(c):
        return not [x for x in (c.get("arguments") or []) if not str(x).startswith("--")]

    def judgeable(service, region):
        if (collected and service not in collected) or (covered_regions and region not in covered_regions):
            return False
        calls = [c for c in evidence.calls.values() if c.get("service") == service and c.get("region") == region and listing(c)]
        return any(c.get("status") == "ok" for c in calls) and not any(c["id"] in truncated for c in calls)
    known = {}
    for (namespace, dim), (_, kinds, value) in DASHBOARD_DIMENSIONS.items():
        known[(namespace, dim)] = {(r["region"], str(value(r))) for r in every if r["type"] in kinds}
    account = inventory.get("account_id")
    for call in evidence.records("cloudwatch", "get-dashboard"):
        data = evidence.data(call) or {}
        name = data.get("DashboardName") or evidence.arg(call, "--dashboard-name")
        if excluded("cloudwatch.dashboard", name):
            continue
        try:
            body = json.loads(data.get("DashboardBody") or "{}")
        except ValueError:
            continue
        missing, judged = [], 0
        for widget in (body.get("widgets") or []) if isinstance(body, dict) else []:
            if not isinstance(widget, dict) or widget.get("type") != "metric" or not isinstance(widget.get("properties"), dict):
                continue
            props = widget["properties"]
            accounts = {str(props.get("accountId") or account)} | {str(o.get("accountId")) for row in props.get("metrics") or [] if isinstance(row, list)
                                                                   for o in row if isinstance(o, dict) and o.get("accountId")}
            if account and accounts != {str(account)}:
                continue  # a cross-account widget names resources of another account
            region = props.get("region") or call.get("region")
            for row in expand_metrics(props.get("metrics")):
                namespace = row[0] if row else None
                pairs = [(row[i], row[i + 1]) for i in range(2, len(row) - 1, 2)]
                for dim, value in pairs:
                    spec = DASHBOARD_DIMENSIONS.get((namespace, dim))
                    if not spec or not isinstance(value, str) or not judgeable(spec[0], region):
                        continue
                    judged += 1
                    if (region, value) not in known[(namespace, dim)]:
                        missing.append(f"widget '{props.get('title') or 'untitled'}' uses {dim}={value}")
        if not judged:
            continue
        missing = list(dict.fromkeys(missing))
        emit("DASHBOARD_UNKNOWN_RESOURCE", "Operational Excellence", None, "WARN" if missing else "PASS",
             (f"Dashboard {name}: {'; '.join(missing[:5])}. " + ("These values match" if len(missing) > 1 else "The value matches") +
              " no resource in the collected inventory, so " + ("those widgets show" if len(missing) > 1 else "the widget shows") + " no data." if missing else
              f"Dashboard {name}: all {judged} resource dimension(s) in its metric widgets name collected resources."),
             "Point each widget at an existing resource (in a stack-managed dashboard, reference the resource attribute, such as the load balancer's "
             "LoadBalancerFullName, instead of a literal) or remove it; an empty panel during an incident reads as missing data rather than a healthy service.",
             "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Dashboard-Body-Structure.html", weight=1, severity="LOW",
             evidence=[call["id"]], target=f"CloudWatch dashboard {name}")

    # UserData that pipes a download into a shell.
    for instance_id, (call, decoded) in sorted(user_data.items()):
        inst = instances_by_id[instance_id]
        found = []
        for line in decoded.splitlines():
            for pattern in PIPE_TO_SHELL:
                m = pattern.search(line)
                if m:
                    found.append((m.group("url").rstrip("\"');"), m.group("shell")))
                    break
        found = list(dict.fromkeys(found))
        if not found:
            continue  # nothing to judge: a script without a piped download is not evidence of a practice
        moving = [u for u, _ in found if moving_ref(u)]
        emit("BOOTSTRAP_UNPINNED_SCRIPT", "Security", inst, "WARN",
             (f"{instance_label(inst)}: UserData pipes {'; '.join(f'{u} into {s}' for u, s in found[:3])}" +
              (" from a moving branch" if moving else "") +
              "; whatever that URL serves at boot runs as root, with no version pin or checksum."),
             "Consider pinning the download to a release version and checking its published checksum before running it, or baking the tool into the "
             "AMI, so a change upstream cannot alter what a new instance runs; a pinned URL with a checksum keeps the convenience of a script install.",
             "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html", severity="MEDIUM",
             evidence=inst["evidence_ids"] + [call["id"]])

    # Non-production options: NAT gateways per AZ and provisioned Aurora capacity.
    if environment == "non-production":
        nats = [n for n in typed("ec2.nat-gateway") if (n["attributes"].get("State") or "available") == "available"]
        for vpc in typed("ec2.vpc"):
            mine = [n for n in nats if n["attributes"].get("VpcId") == vpc["native_id"]]
            if not mine:
                continue
            emit("NAT_PER_AZ_NONPROD", "Cost Optimization", vpc, "WARN" if len(mine) > 1 else "PASS",
                 (f"{vpc['native_id']}: {len(mine)} NAT gateways ({', '.join(n['native_id'] for n in mine[:4])}) in a non-production environment; each is "
                  "billed per hour and per GB processed." if len(mine) > 1 else f"{vpc['native_id']}: one NAT gateway ({mine[0]['native_id']})."),
                 "One option for non-production is to route every private subnet through a single NAT gateway: it saves the hourly charge of the others "
                 "but makes outbound access depend on one AZ. Another is to add VPC endpoints for the busiest services (S3, ECR, CloudWatch Logs) so less "
                 "traffic crosses NAT. One NAT gateway per AZ stays the right choice when tests must survive an AZ failure.",
                 "https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-pricing.html", weight=1, severity="LOW",
                 evidence=vpc["evidence_ids"] + [e for n in mine for e in n["evidence_ids"]])
        for db in typed("rds.instance"):
            a = db["attributes"]
            if not str(a.get("Engine", "")).startswith("aurora") or a.get("DBInstanceClass") in (None, "db.serverless"):
                continue
            if a.get("DBInstanceStatus") not in (None, "available"):
                continue
            emit("AURORA_NONPROD_CAPACITY", "Cost Optimization", db, "WARN",
                 f"{db['native_id']}: provisioned {a.get('DBInstanceClass')} (status {a.get('DBInstanceStatus', 'unknown')} at collection) in a non-production "
                 "environment; a provisioned instance is billed for every hour it runs, used or not.",
                 "Options, depending on how the database is used: Aurora Serverless v2 scales capacity with load (down to a low minimum, or to zero when idle on "
                 "supported versions); a smaller instance class fits a light test load; a stop schedule turns the cluster off outside working hours (a stopped "
                 "cluster starts again by itself after seven days). Keep the provisioned class if load tests need steady, production-like capacity.",
                 "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.html", severity="LOW")
