#!/usr/bin/env python3
"""Quantitative evidence for each Specialist SA Agent recommendation, computed from the run's own evidence.

For every recommendation in 05-modernization/specialist-advice.json the helper resolves what the recommendation cites
(findings, resources, evidence) and writes rows of (metric, value, source) that a reader can check:

- scope: the findings the change addresses (by severity) and the resources it touches (by type);
- availability: the touched components that run in a single Availability Zone or as a single node;
- operations: the EC2 instances among them that the team patches and replaces itself;
- delivery: ECS services deployed by a pipeline versus by other means, and the collected pipeline history (runs,
  failures, median duration, last run), from get-pipeline and list-pipeline-executions;
- dates: support or expiry dates in the cited findings, with the days left after collection;
- cost: the account bill for the touched services in the last closed month (Cost Explorer, account-wide). List prices
  the specialist cited are shown by the report next to these rows;
- one customer-input row: the figure only the owner knows (hours per deployment, hours per host, cost of an hour of
  outage), with the count it multiplies. It has no value and is never added to a total.

Nothing is estimated: no person-hours, no savings percentages, no productivity figures. A metric whose evidence was not
collected is left out rather than guessed. The report (assessment_report.py) calls `compute()` at render time and writes
06-assessment/benefits.json; this command writes 05-modernization/benefits.json for inspection.

  python3 benefits.py --run-dir RUN_DIR [--output FILE]
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

SCHEMA = "1.0"
SEVERITY_ORDER = ("HIGH", "MEDIUM", "LOW", "INFO")
TYPE_WORDS = {"ecs.service": "ECS service", "ecs.cluster": "ECS cluster", "eks.cluster": "EKS cluster", "ec2.instance": "EC2 instance",
              "rds.cluster": "Aurora/RDS cluster", "rds.instance": "RDS instance", "elasticache.replication-group": "ElastiCache group",
              "elasticache.cluster": "ElastiCache node", "elbv2.load-balancer": "load balancer", "secretsmanager.secret": "secret",
              "codepipeline.pipeline": "pipeline", "codebuild.project": "build project", "ecr.repository": "ECR repository", "s3.bucket": "S3 bucket",
              "lambda.function": "Lambda function", "dynamodb.table": "DynamoDB table", "sns.topic": "SNS topic", "sqs.queue": "SQS queue",
              "cloudwatch.alarm": "alarm", "logs.group": "log group", "autoscaling.group": "Auto Scaling group", "bedrock.agent": "Bedrock agent",
              "bedrock.guardrail": "Bedrock guardrail", "bedrock.knowledge-base": "Bedrock knowledge base"}
# Resource type (or type prefix) -> the Cost Explorer services that bill it. By type rather than by service prefix, so an
# EC2 instance is not shown with the VPC line (NAT gateways and endpoints bill there).
BILL_BY_TYPE = {"ec2.instance": ("Amazon Elastic Compute Cloud - Compute", "EC2 - Other"), "autoscaling.group": ("Amazon Elastic Compute Cloud - Compute", "EC2 - Other"),
                "ec2.nat-gateway": ("Amazon Virtual Private Cloud", "EC2 - Other"), "ec2.vpc-endpoint": ("Amazon Virtual Private Cloud",),
                "rds": ("Amazon Relational Database Service",), "elasticache": ("Amazon ElastiCache",), "ecs": ("Amazon Elastic Container Service",),
                "eks": ("Amazon Elastic Container Service for Kubernetes", "Amazon Elastic Kubernetes Service"),
                "elbv2": ("Elastic Load Balancing", "Amazon Elastic Load Balancing"), "lambda": ("AWS Lambda",), "s3": ("Amazon Simple Storage Service",),
                "dynamodb": ("Amazon DynamoDB",), "cloudwatch": ("AmazonCloudWatch",), "logs": ("AmazonCloudWatch",), "ecr": ("Amazon EC2 Container Registry (ECR)",),
                "secretsmanager": ("AWS Secrets Manager",), "sqs": ("Amazon Simple Queue Service",), "sns": ("Amazon Simple Notification Service",),
                "opensearch": ("Amazon OpenSearch Service",), "bedrock": ("Amazon Bedrock",), "codepipeline": ("AWS CodePipeline",), "codebuild": ("CodeBuild", "AWS CodeBuild"),
                "cloudfront": ("Amazon CloudFront",), "wafv2": ("AWS WAF",), "kms": ("AWS Key Management Service",)}
# Rules whose observation carries a date the business cannot move (the report's "Has a date" list uses the same set).
DATED_RULES = {"EKS_VERSION_SUPPORT": "Kubernetes standard support ends", "RDS_ENGINE_SUPPORT": "Database engine standard support ends",
               "RDS_ENGINE_VERSION_SUPPORT": "Database engine standard support ends", "LAMBDA_RUNTIME_DEPRECATION": "Lambda runtime deprecation",
               "ACM_CERTIFICATE_EXPIRY": "Certificate expires", "EC2_OS_END_OF_LIFE": "Operating system end of life"}
OBSERVABILITY_RULES = re.compile(r"^(ALARM_|LOG_|DASHBOARD_|OPS_|EC2_CLOUDWATCH|ECS_CONTAINER_INSIGHTS|EKS_OBSERVABILITY|EKS_CONTROL_PLANE)")
DELIVERY_RULES = re.compile(r"^(CI_|DELIVERY_|ECS_DEPLOYMENT|IMAGE_TAG|IAC_|STACK_DRIFT)")
DELIVERY_TYPES = {"codepipeline.pipeline", "codebuild.project", "codecommit.repository", "ecr.repository"}
CUSTOMER = "owner to provide"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def judged_snapshot(run, findings):
    pointer = (findings.get("as_is_snapshot") or {}).get("path")
    if pointer and (run / pointer / "inventory.json").is_file():
        return run / pointer
    return run / "03-as-is-enriched" if (run / "03-as-is-enriched/inventory.json").is_file() else run / "03-as-is"


def fingerprint(rec):
    """Identifies a recommendation across files: its title and references."""
    return hashlib.sha256(json.dumps([rec.get("title"), sorted(rec.get("refs") or [])], ensure_ascii=False).encode()).hexdigest()[:16]


def plural(n, word):
    if n == 1:
        return f"1 {word}"
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy", "uy")):
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}es" if word.endswith(("s", "x", "sh", "ch")) else f"{n} {word}s"


def short_name(r):
    a = r.get("attributes") or {}
    for key in ("serviceName", "DBInstanceIdentifier", "DBClusterIdentifier", "ReplicationGroupId", "CacheClusterId", "LoadBalancerName", "FunctionName", "name"):
        if a.get(key):
            return str(a[key])
    tags = {t.get("Key"): t.get("Value") for t in a.get("Tags") or a.get("TagList") or [] if isinstance(t, dict)}
    return tags.get("Name") or r.get("name") or str(r.get("native_id", "")).rsplit("/", 1)[-1]


class Evidence:
    """The run's evidence, indexed once: inventory, findings, supplementary records, the bill and list prices."""

    def __init__(self, run):
        self.run = Path(run)
        self.findings = read_json(self.run / "04-review/findings.json")
        self.snapshot = judged_snapshot(self.run, self.findings)
        self.inventory = read_json(self.snapshot / "inventory.json")
        manifest = self.snapshot / "collection-manifest.json"
        self.manifest = read_json(manifest) if manifest.is_file() else {}
        self.collected = str(self.manifest.get("completed_at") or "")[:10] or None
        self.resources = {r["id"]: r for r in self.inventory.get("resources", [])}
        self.alias = {}
        for r in self.inventory.get("resources", []):
            self.alias.setdefault(str(r.get("native_id")), r["id"])
            for key, value in (r.get("attributes") or {}).items():
                if isinstance(value, str) and value.startswith("arn:") and key.lower().endswith("arn"):
                    self.alias.setdefault(value, r["id"])
        self.checks = {c["id"]: c for c in self.findings.get("checks", [])}
        scope = self.snapshot / "scope-resources.json"
        self.scope_ids = {r["id"] for r in read_json(scope).get("resources", [])} if scope.is_file() else None
        self.supplementary = self.load_supplementary()
        self.bill = next((r for r in self.inventory.get("resources", []) if r.get("type") == "ce.cost-by-service"
                          and (r.get("collection") or {}).get("status", "ok") == "ok"), None)

    def load_supplementary(self):
        """(operation, arguments, evidence id, parsed response) for the records stage 3 collected beside the inventory."""
        out = []
        for folder in (self.run / "03-supplementary", self.snapshot / "supplementary"):
            manifest = folder / "supplementary-manifest.json"
            if not manifest.is_file():
                continue
            for record in read_json(manifest).get("records", []):
                raw = folder / str(record.get("raw_path") or "")
                if record.get("status") != "ok" or not raw.is_file() or record.get("raw_format", "json") != "json":
                    continue
                try:
                    data = read_json(raw)
                except ValueError:
                    continue
                out.append((record.get("operation"), record.get("arguments") or [], record.get("id"), data))
            break
        return out

    def in_scope(self, r):
        return r.get("level") != "account" and (self.scope_ids is None or r["id"] in self.scope_ids)

    def resource(self, ref):
        rid = ref if ref in self.resources else self.alias.get(ref)
        return self.resources.get(rid) if rid else None

    def subnet_az(self, subnet_id):
        r = self.resources.get(self.alias.get(subnet_id, ""))
        return (r or {}).get("attributes", {}).get("AvailabilityZone")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def touched(ev, rec):
    """(findings, resources) the recommendation cites: findings by id, resources by id and through the cited findings."""
    findings, resources = [], {}
    for ref in rec.get("refs") or []:
        if ref in ev.checks:
            c = ev.checks[ref]
            findings.append(c)
            r = ev.resource(c.get("resource_id") or "") or ev.resource(c.get("target") or "")
            if r:
                resources[r["id"]] = r
        else:
            r = ev.resource(ref)
            if r:
                resources[r["id"]] = r
    return findings, list(resources.values())


def single_az(ev, r):
    """The single Availability Zone (or 'one node') a component runs in, or None when it spans zones or it is unknown."""
    a, t = r.get("attributes") or {}, r.get("type")
    if t == "rds.cluster":
        zones = set()
        for m in a.get("DBClusterMembers") or []:
            inst = ev.resource(m.get("DBInstanceIdentifier", ""))
            zone = (inst or {}).get("attributes", {}).get("AvailabilityZone")
            if zone:
                zones.add(zone)
        return next(iter(zones)) if len(zones) == 1 and a.get("MultiAZ") is not True else None
    if t == "rds.instance":
        if a.get("DBClusterIdentifier"):
            return None  # judged at cluster level
        return a.get("AvailabilityZone") if a.get("MultiAZ") is False else None
    if t == "elasticache.replication-group":
        members = a.get("MemberClusters") or []
        if str(a.get("MultiAZ", "")).lower() != "enabled" and (len(members) <= 1 or str(a.get("AutomaticFailover", "")).lower() != "enabled"):
            node = ev.resource(members[0]) if members else None
            return (node or {}).get("attributes", {}).get("PreferredAvailabilityZone") or "one node"
        return None
    if t == "elasticache.cluster":
        return a.get("PreferredAvailabilityZone") if not a.get("ReplicationGroupId") and (a.get("NumCacheNodes") or 1) == 1 else None
    if t == "ec2.instance":
        in_asg = any(i.get("InstanceId") == a.get("InstanceId") for g in ev.inventory.get("resources", []) if g.get("type") == "autoscaling.group"
                     for i in (g.get("attributes") or {}).get("Instances") or [])
        return None if in_asg else (a.get("Placement") or {}).get("AvailabilityZone")
    if t == "ecs.service":
        subnets = ((a.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}).get("subnets") or []
        zones = {ev.subnet_az(s) for s in subnets} - {None}
        if len(zones) == 1:
            return next(iter(zones))
        return "one task" if a.get("desiredCount") == 1 else None
    if t == "elbv2.load-balancer":
        zones = [z.get("ZoneName") for z in a.get("AvailabilityZones") or []]
        return zones[0] if len(zones) == 1 else None
    return None


def deployment_facts(ev):
    """ECS services named by a pipeline's deploy action, and per-pipeline history from list-pipeline-executions."""
    deployed, pipelines = {}, {}
    for operation, args, eid, data in ev.supplementary:
        if operation == "get-pipeline":
            name = (data.get("pipeline") or {}).get("name")
            for stage in (data.get("pipeline") or {}).get("stages") or []:
                for action in stage.get("actions") or []:
                    config = action.get("configuration") or {}
                    if (action.get("actionTypeId") or {}).get("provider") in {"ECS", "CodeDeployToECS"} and config.get("ServiceName"):
                        deployed.setdefault(config["ServiceName"], (name, eid))
        elif operation == "list-pipeline-executions":
            name = args[args.index("--pipeline-name") + 1] if "--pipeline-name" in args else "?"
            runs = data.get("pipelineExecutionSummaries") or []
            durations = []
            for x in runs:
                try:
                    start = datetime.fromisoformat(str(x["startTime"]))
                    end = datetime.fromisoformat(str(x["lastUpdateTime"]))
                    if x.get("status") == "Succeeded":
                        durations.append((end - start).total_seconds() / 60)
                except (KeyError, ValueError):
                    pass
            counts = Counter(x.get("status") for x in runs)
            starts = sorted(str(x.get("startTime", ""))[:10] for x in runs if x.get("startTime"))
            pipelines[name] = {"runs": len(runs), "succeeded": counts.get("Succeeded", 0), "failed": counts.get("Failed", 0) + counts.get("Stopped", 0),
                               "median_minutes": round(statistics.median(durations), 1) if durations else None, "first": starts[0] if starts else None,
                               "last": starts[-1] if starts else None, "evidence": eid}
    return deployed, pipelines


def bill_row(ev, resources):
    months = ((ev.bill or {}).get("attributes") or {}).get("months") or []
    if not months or not resources:
        return None
    names = set()
    for r in resources:
        names |= set(BILL_BY_TYPE.get(r["type"]) or BILL_BY_TYPE.get(r["type"].split(".")[0]) or ())
    last = months[-1]
    lines = [(s["service"], s["usd"]) for s in last.get("services") or [] if s["service"] in names]
    if not lines:
        return None
    total = sum(usd for _, usd in lines)
    return {"metric": "Account bill for the services these resources use, " + str(last.get("start", ""))[:7],
            "value": f"${total:,.0f}/month", "kind": "cost",
            "source": "Cost Explorer UnblendedCost for the whole account (" + ", ".join(f"{s} ${u:,.0f}" for s, u in lines[:4])
                      + "); other workloads in the account are included and the workload's share is not separated",
            "refs": [ev.bill["id"]]}


def observability_row(ev):
    """Alarms in scope, how many have an action, and whether the topics they notify have a confirmed subscriber."""
    alarms = [r for r in ev.inventory.get("resources", []) if r["type"] == "cloudwatch.alarm" and ev.in_scope(r)]
    topics = [r for r in ev.inventory.get("resources", []) if r["type"] == "sns.topic" and ev.in_scope(r)]
    if not alarms and not topics:
        return None
    acting = sum(1 for r in alarms if (r.get("attributes") or {}).get("AlarmActions"))
    subscribed = sum(1 for r in topics if int((r.get("attributes") or {}).get("SubscriptionsConfirmed") or 0) > 0)
    return {"metric": "Alarms and notification topics in scope", "value": f"{len(alarms)} alarm(s), {acting} with an action; {subscribed} of {len(topics)} topic(s) with a confirmed subscriber",
            "kind": "count", "source": "inventory.json: cloudwatch describe-alarms, sns get-topic-attributes", "refs": [r["id"] for r in alarms + topics]}


def days_between(a, b):
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def rows_for(ev, rec, pathway, deployed, pipelines):
    findings, resources = touched(ev, rec)
    rows = []
    judged = [c for c in findings if c["status"] in {"FAIL", "WARN"}]
    if findings:
        sev = Counter(c.get("severity") for c in judged)
        parts = ", ".join(f"{sev[s]} {s.lower()}" for s in SEVERITY_ORDER if sev.get(s))
        open_q = sum(1 for c in findings if c["status"] == "UNKNOWN")
        value = f"{len(judged)} failed or warned" + (f" ({parts})" if parts else "") + (f"; {open_q} open question(s)" if open_q else "")
        rows.append({"metric": "Findings this change addresses", "value": value, "kind": "count",
                     "source": "findings.json: " + ", ".join(c["id"] for c in findings[:8]) + (f" +{len(findings) - 8}" if len(findings) > 8 else ""),
                     "refs": [c["id"] for c in findings]})
    if resources:
        kinds = Counter(TYPE_WORDS.get(r["type"], r["type"]) for r in resources)
        rows.append({"metric": "Resources it touches", "value": f"{len(resources)}: " + ", ".join(plural(n, k) for k, n in kinds.most_common()),
                     "kind": "count", "source": "inventory.json (configuration read by API)", "refs": [r["id"] for r in resources]})
    single = [(r, single_az(ev, r)) for r in resources]
    single = [(r, z) for r, z in single if z]
    if single:
        rows.append({"metric": "Single-AZ or single-node components among them",
                     "value": f"{len(single)}: " + ", ".join(f"{short_name(r)} ({z})" for r, z in single[:4]) + (f" +{len(single) - 4}" if len(single) > 4 else ""),
                     "kind": "count", "source": "inventory.json: AvailabilityZone, MultiAZ, cluster members, subnets, node count",
                     "refs": [r["id"] for r, _ in single]})
    hosts = [r for r in resources if r["type"] == "ec2.instance"]
    if hosts:
        types = Counter((r.get("attributes") or {}).get("InstanceType") or "?" for r in hosts)
        rows.append({"metric": "EC2 instances the team patches and replaces itself", "value": f"{len(hosts)} (" + ", ".join(f"{t} × {n}" for t, n in types.most_common()) + ")",
                     "kind": "count", "source": "inventory.json: ec2 describe-instances", "refs": [r["id"] for r in hosts]})
    delivery = pathway == "modern-devops" or any(r["type"] in DELIVERY_TYPES for r in resources) or any(DELIVERY_RULES.search(c.get("rule_id", "")) for c in findings)
    services = [r for r in ev.inventory.get("resources", []) if r["type"] == "ecs.service" and ev.in_scope(r)]
    without = []
    if delivery and services and any(op == "get-pipeline" for op, _, _, _ in ev.supplementary):
        names = [short_name(r) for r in services]
        with_pipeline = [n for n in names if n in deployed]
        without = [n for n in names if n not in deployed]
        rows.append({"metric": "ECS services deployed by a pipeline", "value": f"{len(with_pipeline)} of {len(names)}"
                     + (f" ({', '.join(with_pipeline)})" if with_pipeline else "") + (f"; without: {', '.join(without)}" if without else ""),
                     "kind": "count", "source": "codepipeline get-pipeline deploy actions (" + ", ".join(sorted({e for _, e in deployed.values()})) + ")" if deployed else "codepipeline get-pipeline: no ECS deploy action",
                     "refs": sorted({e for _, e in deployed.values()})})
    if delivery:
        for name, p in sorted(pipelines.items()):
            if not p["runs"]:
                continue
            value = f"{p['runs']} run(s), {p['failed']} failed" + (f", successful runs take {p['median_minutes']:g} min (median)" if p["median_minutes"] is not None else "") + (f", last {p['last']}" if p["last"] else "")
            rows.append({"metric": f"Pipeline history {name}", "value": value, "kind": "duration",
                         "source": f"codepipeline list-pipeline-executions, latest {p['runs']} ({p['evidence']}); {p['first']} to {p['last']}", "refs": [p["evidence"]]})
    for c in findings:
        if c.get("rule_id") not in DATED_RULES or c["status"] not in {"FAIL", "WARN"}:
            continue
        dates = re.findall(r"\b(20\d\d-\d\d-\d\d)\b", str(c.get("observation", "")))
        if dates:
            date = max(dates) if c["status"] == "WARN" else min(dates)
            left = days_between(ev.collected, date)
            rows.append({"metric": DATED_RULES[c["rule_id"]], "value": date + (f" ({left} days after collection)" if left is not None and left >= 0 else " (already passed)" if left is not None else ""),
                         "kind": "date", "source": f"{c['id']}: {str(c.get('observation', ''))[:120]}", "refs": [c["id"]]})
    if pathway == "modern-devops" and any(OBSERVABILITY_RULES.search(c.get("rule_id", "")) for c in findings):
        row = observability_row(ev)
        if row:
            rows.append(row)
    bill = bill_row(ev, resources)
    if bill:
        rows.append(bill)
    # One figure only the owner knows, with the count it multiplies.
    if without:
        rows.append({"metric": "Hours per manual deployment × deployments per month", "value": CUSTOMER, "kind": "input_needed",
                     "source": f"customer input; multiplies the {len(without)} service(s) without a pipeline; not added to any total", "refs": []})
    elif hosts:
        rows.append({"metric": "Hours per host per month for patching and replacement", "value": CUSTOMER, "kind": "input_needed",
                     "source": f"customer input; multiplies the {len(hosts)} EC2 instance(s) above; not added to any total", "refs": []})
    elif single:
        rows.append({"metric": "Cost of one hour without these components", "value": CUSTOMER, "kind": "input_needed",
                     "source": "customer input; recovery time is not measured (see the restore and failover questions); not added to any total", "refs": []})
    return rows


def compute(run, specialists=None):
    """Evidence rows per recommendation. `specialists` is the merged list (as the report loads it); default: read
    05-modernization/specialist-advice.json."""
    run = Path(run)
    ev = Evidence(run)
    if specialists is None:
        path = run / "05-modernization/specialist-advice.json"
        specialists = read_json(path).get("specialists", []) if path.is_file() else []
    deployed, pipelines = deployment_facts(ev)
    out = []
    for sp in specialists:
        for i, rec in enumerate(sp.get("recommendations") or [], 1):
            out.append({"specialist": sp["specialist"], "index": i, "title": rec.get("title"), "fingerprint": fingerprint(rec),
                        "evidence": rows_for(ev, rec, rec.get("pathway"), deployed, pipelines)})
    return {"schema_version": SCHEMA, "run_id": ev.inventory.get("run_id"), "account_id": ev.inventory.get("account_id"),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "collected": ev.collected,
            "method": "Counts, dates and durations computed from the run's evidence; the account bill from Cost Explorer; list prices are the "
                      "specialists' cited list_price.py ids. No person-hours, savings or productivity figures are estimated; figures only the "
                      "owner knows are customer-input rows without a value.",
            "inputs": {"inventory": str(ev.snapshot.relative_to(run) / "inventory.json"), "supplementary_records": len(ev.supplementary),
                       "cost_explorer": bool(ev.bill), "pipelines_with_history": len(pipelines)},
            "recommendations": out}


def attach(specialists, result):
    """Put each recommendation's rows on it as `evidence` (matched by specialist, position and fingerprint)."""
    by_key = {(r["specialist"], r["index"]): r for r in result.get("recommendations", [])}
    for sp in specialists:
        for i, rec in enumerate(sp.get("recommendations") or [], 1):
            row = by_key.get((sp["specialist"], i))
            if row and row["fingerprint"] == fingerprint(rec):
                rec["evidence"] = row["evidence"]
    return specialists


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="default: RUN_DIR/05-modernization/benefits.json")
    args = parser.parse_args()
    try:
        result = compute(args.run_dir)
        out = args.output or args.run_dir / "05-modernization/benefits.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows = sum(len(r["evidence"]) for r in result["recommendations"])
        print(json.dumps({"status": "written", "output": str(out), "recommendations": len(result["recommendations"]), "rows": rows,
                          "inputs": result["inputs"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Benefits stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
