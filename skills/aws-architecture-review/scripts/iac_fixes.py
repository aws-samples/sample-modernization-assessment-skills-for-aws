"""Template changes (iac_changes) for baseline checks whose fix is one CloudFormation property.

For a FAIL or WARN baseline check whose resource is declared in a collected stack, the stack's `list-stack-resources`
record maps the resource's physical id to its logical id and type, and the stack's `get-template` record supplies the
literal current value. Every change is validated with review.validate_iac_changes before it is attached; a change that
does not validate (a template shape this module does not read, a property set in an unexpected place) is dropped
without stopping the review. A manual check that replaces the baseline check keeps its own iac_changes.
"""
import json
import re

# rule -> (CloudFormation resource types, [(property, proposed, note, is_resource_attribute)])
FIXES = {
    "ECR_SCAN_ON_PUSH": (("AWS::ECR::Repository",), [
        ("ImageScanningConfiguration", "{ ScanOnPush: true }",
         "Registry-level enhanced scanning (Amazon Inspector) is the alternative and scans continuously.", False)]),
    "ECR_TAG_IMMUTABILITY": (("AWS::ECR::Repository",), [
        ("ImageTagMutability", "IMMUTABLE", "A pipeline that re-pushes a fixed tag such as latest has to tag by commit first.", False)]),
    "LOG_RETENTION": (("AWS::Logs::LogGroup",), [
        ("RetentionInDays", "30", "30 days is an example; the period follows the owner's retention policy.", False)]),
    "ECS_DEPLOYMENT_CIRCUIT_BREAKER": (("AWS::ECS::Service",), [
        ("DeploymentConfiguration", "DeploymentCircuitBreaker: { Enable: true, Rollback: true }", "", False)]),
    "ECS_CONTAINER_INSIGHTS": (("AWS::ECS::Cluster",), [
        ("ClusterSettings", "[{ Name: containerInsights, Value: enhanced }]", "", False)]),
    "EKS_CONTROL_PLANE_LOGGING": (("AWS::EKS::Cluster",), [
        ("Logging", "ClusterLogging: { EnabledTypes: [{ Type: api }, { Type: audit }, { Type: authenticator }] }",
         "audit and authenticator are the minimum for access review; give the /aws/eks/<cluster>/cluster log group a retention period.", False)]),
    "RDS_BACKUPS": (("AWS::RDS::DBCluster", "AWS::RDS::DBInstance"), [
        ("BackupRetentionPeriod", "7", "7 days is an example; match the recovery point objective (1 to 35 days).", False)]),
    "RDS_DELETION_PROTECTION": (("AWS::RDS::DBCluster", "AWS::RDS::DBInstance"), [
        ("DeletionProtection", "true", "", False),
        ("DeletionPolicy", "Snapshot", "DeletionPolicy is a resource attribute; Snapshot keeps a final snapshot when the stack or the resource is "
         "deleted (set UpdateReplacePolicy the same way).", True)]),
    "ELASTICACHE_TRANSIT_ENCRYPTION": (("AWS::ElastiCache::ReplicationGroup",), [
        ("TransitEncryptionEnabled", "true (with an AuthToken or an RBAC user group)",
         "Clients must connect with TLS; an existing group moves through TransitEncryptionMode preferred, then required.", False)]),
    "EKS_NODE_SCALING": (("AWS::EKS::Nodegroup",), [
        ("ScalingConfig", "MaxSize above MinSize, for example MinSize 2, DesiredSize 2, MaxSize 4",
         "A node autoscaler (Cluster Autoscaler or Karpenter) uses the headroom.", False)]),
    "ALARM_NOTIFICATION_TARGET": (("AWS::SNS::Topic",), [
        ("Subscription", "an email or chat subscription for the owning team",
         "A list of { Endpoint, Protocol } entries, or a separate AWS::SNS::Subscription resource.", False)]),
    "EKS_SECRETS_ENCRYPTION": (("AWS::EKS::Cluster",), [
        ("EncryptionConfig", "[{ Resources: [secrets], Provider: { KeyArn: <KMS key ARN> } }]",
         "Secrets encryption cannot be turned off once enabled; check the property's update behaviour before applying it to a running cluster.", False)]),
    "DB_DIAGNOSTICS": (("AWS::RDS::DBInstance",), [
        ("EnablePerformanceInsights", "true",
         "Enhanced Monitoring (MonitoringInterval with MonitoringRoleArn) and log exports (EnableCloudwatchLogsExports, on the DBCluster for Aurora) are separate properties.",
         False)]),
}


def indent(line):
    return len(line) - len(line.lstrip())


def strip_comment(value):
    """Drop a trailing YAML comment unless the value holds quotes (a '#' inside a string is not a comment)."""
    if "'" in value or '"' in value:
        return value.strip()
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def yaml_current(block, prop, attribute):
    """(understood, current) for a property (or resource attribute) of one YAML resource block."""
    lines = block.splitlines()
    body = [l for l in lines[1:] if l.strip() and not l.lstrip().startswith("#")]
    if not body:
        return True, None
    child = indent(body[0])
    key = re.compile(rf"^\s*[\"']?{re.escape(prop)}[\"']?\s*:(.*)$")
    if attribute:
        scope = [l for l in body if indent(l) == child]
        return True, next((strip_comment(m.group(1)) or None for l in scope for m in [key.match(l)] if m), None)
    start = next((i for i, l in enumerate(body) if indent(l) == child and re.match(r"^\s*Properties\s*:(.*)$", l)), None)
    if start is None:
        return True, None
    inline = strip_comment(body[start].split(":", 1)[1])
    if inline:  # flow mapping such as Properties: { Roles: [...] }
        return (False, None) if re.search(rf"\b{re.escape(prop)}\s*:", inline) else (True, None)
    section = []
    for line in body[start + 1:]:
        if indent(line) <= child:
            break
        section.append(line)
    if not section:
        return True, None
    level = indent(section[0])
    for i, line in enumerate(section):
        if indent(line) != level:
            continue
        m = key.match(line)
        if not m:
            continue
        value = strip_comment(m.group(1))
        if value:
            return True, value
        nested = next((l.strip() for l in section[i + 1:] if indent(l) > level), None)
        return (nested is not None), nested
    return True, None


def json_current(body, logical_id, prop, attribute):
    resource = (body.get("Resources") or {}).get(logical_id)
    if not isinstance(resource, dict):
        return False, None
    scope = resource if attribute else (resource.get("Properties") or {})
    if not isinstance(scope, dict):
        return False, None
    return True, (json.dumps(scope[prop], sort_keys=True) if prop in scope else None)


def keys(value):
    """Identifiers a physical resource id may take for a native id, name or ARN."""
    out = set()
    if not isinstance(value, str) or not value:
        return out
    out.add(value)
    if value.startswith("arn:"):
        tail = value.split(":", 5)[5] if value.count(":") >= 5 else ""
        out.add(tail)
        if ":" in tail:
            out.add(tail.rsplit(":", 1)[-1])
        path = tail.split("/")[1:]
        for n in range(1, len(path) + 1):
            out.add("/".join(path[:n]))
    return out


def stack_index(evidence):
    """physical id -> [(resource type, region, stack, logical id)] from list-stack-resources records, and stack -> templates."""
    index, templates = {}, {}
    for call in evidence.records("cloudformation", "list-stack-resources"):
        stack = evidence.arg(call, "--stack-name") or (call.get("provenance") or {}).get("origin")
        for r in (evidence.data(call) or {}).get("StackResourceSummaries") or []:
            if r.get("PhysicalResourceId") and r.get("LogicalResourceId"):
                index.setdefault(r["PhysicalResourceId"], []).append((r.get("ResourceType"), call.get("region"), stack, r["LogicalResourceId"]))
    for call in evidence.records("cloudformation", "get-template"):
        stack = evidence.arg(call, "--stack-name")
        if stack:
            stage = evidence.arg(call, "--template-stage") or "Original"
            templates.setdefault(stack, []).append((0 if stage == "Original" else 1, call))
    return index, {s: [c for _, c in sorted(v, key=lambda x: x[0])] for s, v in templates.items()}


def attach(checks, resources, evidence, validate, template_block):
    """Attach validated iac_changes to baseline checks in place; returns the number of changes attached."""
    index, templates = stack_index(evidence)
    if not index or not templates:
        return 0
    by_id = {r["id"]: r for r in resources}
    attached = 0
    for check in checks:
        fix = FIXES.get(check.get("rule_id"))
        if not fix or check.get("status") not in ("FAIL", "WARN") or check.get("iac_changes"):
            continue
        types, changes = fix
        resource = by_id.get(check.get("resource_id"))
        if resource and resource["type"] == "rds.instance" and check["rule_id"] == "RDS_BACKUPS" and resource["attributes"].get("DBClusterIdentifier"):
            continue  # an Aurora instance takes its backups from the cluster
        candidates = set()
        if resource:
            for value in [resource["native_id"], resource.get("name")] + [resource["attributes"].get(k) for k in (
                    "DBClusterIdentifier", "DBInstanceIdentifier", "clusterName", "serviceName", "name", "logGroupName", "TopicArn")]:
                candidates |= keys(value)
        candidates |= keys(check.get("target"))
        region = resource["region"] if resource else None
        matches = set()
        for candidate in sorted(candidates, key=len, reverse=True):  # the most specific identifier wins
            matches = {(stack, logical) for kind, where, stack, logical in index.get(candidate, [])
                       if kind in types and (region is None or where in (None, region))}
            if matches:
                break
        if len(matches) != 1:
            continue
        stack, logical_id = next(iter(matches))
        for call in templates.get(stack, []):
            body = (evidence.data(call) or {}).get("TemplateBody")
            if isinstance(body, dict):
                read = lambda prop, attribute: json_current(body, logical_id, prop, attribute)  # noqa: E731
            else:
                block = template_block(body, logical_id)
                if block is None:
                    continue
                read = lambda prop, attribute: yaml_current(block, prop, attribute)  # noqa: E731
            made = []
            for prop, proposed, note, attribute in changes:
                understood, current = read(prop, attribute)
                if not understood:
                    continue
                change = {"stack": stack, "logical_id": logical_id, "property": prop, "current": current,
                          "proposed": proposed, "evidence_id": call["id"], "note": note}
                trial = dict(check, evidence_ids=list(check["evidence_ids"]), iac_changes=[change])
                try:
                    validate(trial)
                except (ValueError, KeyError, TypeError, OSError):
                    continue
                made.append(change)
            if made:
                check["iac_changes"] = made
                if call["id"] not in check["evidence_ids"]:
                    check["evidence_ids"].append(call["id"])
                attached += len(made)
                break
    return attached
