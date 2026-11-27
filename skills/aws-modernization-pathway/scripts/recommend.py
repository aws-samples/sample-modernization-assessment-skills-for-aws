#!/usr/bin/env python3
"""Validate and rank evidence-linked modernization candidates; no AWS API calls."""
import argparse
import base64
from datetime import datetime, timezone
import html
import json
import re
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_kit  # noqa: E402  (shared visual report kit, standard library only)

PATHWAYS = {
    "container": "Move to Container", "serverless": "Move to Serverless", "managed-services": "Move to Managed Services",
    "managed-database": "Move to Managed Database", "managed-analytics": "Move to Managed Analytics",
    "genai": "Move to GenAI", "open-source": "Move to Open Source",
    "modern-devops": "Move to Modern DevOps", "cloud-native": "Move to Cloud Native",
}
# The seven modernization directions, in the order the report leads with them, and the two foundations: the delivery and
# operations practices that make the moves safe. Foundations are shown after the directions and not counted among them.
DIRECTIONS = ("container", "serverless", "managed-services", "managed-database", "managed-analytics", "genai", "open-source")
FOUNDATIONS = ("modern-devops", "cloud-native")
# Pathways added after catalog 2.0. A candidates file written before them still loads: each one is inserted as not assessed,
# with a rationale built only from the findings, the inventory and the specialist reviews (see inserted_pathway).
ADDED_PATHWAYS = ("managed-services", "open-source")
WEIGHTS = {"business_value": 25, "technical_fit": 25, "operating_model": 15,
           "risk_manageability": 15, "delivery_feasibility": 10, "cost_confidence": 10}
STATUSES = {"candidate", "conditional", "not_recommended", "not_assessed"}
AI_CATALOG = json.loads((Path(__file__).resolve().parents[1] / "assets/ai-options.json").read_text(encoding="utf-8"))
AI_OPTIONS = {o["id"]: o for o in AI_CATALOG["options"]}
PLATFORM_CATALOG = json.loads((Path(__file__).resolve().parents[1] / "assets/platform-options.json").read_text(encoding="utf-8"))
PLATFORM_GROUPS = {g["pathway"]: g for g in PLATFORM_CATALOG["groups"]}
PLATFORM_READINESS = ("business_fit", "security", "operations", "availability", "cost")
AI_READINESS = ("business_fit", "data", "security", "quality", "operations", "availability", "cost")
AI_READINESS_STATES = {"confirmed", "unknown", "blocked"}
AI_READINESS_LABELS = {
    "business_fit": "Business fit", "data": "Data", "security": "Security", "quality": "Quality",
    "operations": "Operations", "availability": "Availability", "cost": "Cost",
    "model_compatibility": "Model compatibility", "benchmark": "Measured comparison",
    "telemetry_compatibility": "Instrumentation/protocol compatibility", "pipeline_resilience": "Collector failure handling",
    "data_source_access": "Data source access", "alert_ownership": "Alarm ownership",
    "cardinality_capacity": "Time series and capacity", "delivery_semantics": "Delivery and ordering semantics",
    "consumer_safety": "Consumer idempotency and backpressure", "replay_recovery": "Replay and recovery",
    "kafka_compatibility": "Kafka compatibility", "partition_capacity": "Partitions and capacity",
}
AI_STATUS_LABELS = {"candidate": "Verified candidate", "conditional": "Conditional",
                    "not_recommended": "Not recommended", "not_assessed": "Unassessed"}
AI_CATEGORY_LABELS = {"managed-agent": "Managed agent", "foundation-model-platform": "Model platform",
                      "agent-platform": "Agent operations platform", "task-ai-service": "Task AI service",
                      "ml-platform": "ML platform", "ai-accelerator": "AI accelerator",
                      "telemetry": "Instrumentation and collection", "visualization": "Visualization", "metrics": "Metrics",
                      "streaming": "Streaming", "queue": "Work queue", "event-routing": "Event routing", "pubsub": "Publish/subscribe"}
PUBLIC_SOURCES = {
    "container": "https://docs.aws.amazon.com/decision-guides/latest/containers-on-aws-how-to-choose/containers-on-aws-how-to-choose.html",
    "serverless": "https://docs.aws.amazon.com/lambda/latest/dg/welcome.html",
    "managed-services": "https://docs.aws.amazon.com/prescriptive-guidance/latest/large-migration-guide/migration-strategies.html",
    "managed-database": "https://docs.aws.amazon.com/prescriptive-guidance/latest/strategy-database-migration/welcome.html",
    "managed-analytics": "https://docs.aws.amazon.com/decision-guides/latest/analytics-on-aws-how-to-choose/analytics-on-aws-how-to-choose.html",
    "genai": "https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html",
    "open-source": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_SchemaConversion.html",
    "cloud-native": "https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html",
    "modern-devops": "https://docs.aws.amazon.com/wellarchitected/latest/devops-guidance/dl.cd.6-refine-delivery-pipelines-using-metrics-for-continuous-improvement.html",
}
QUESTIONS = {
    "container": "Verify application processes, state, packaging constraints and operating capabilities",
    "serverless": "Verify event/request patterns, execution constraints, state and database connection requirements",
    "managed-services": ("Verify what each self-managed host or hand-run operation does (cache, broker, search, scheduler, self-run cluster, bastion access), "
                         "the state it keeps and which AWS managed service could take it over"),
    "managed-database": "Verify database location, engine, schema, SQL/extension compatibility and RTO/RPO",
    "managed-analytics": "Verify analytics use cases, data classification, consumption patterns and freshness requirements",
    "genai": "Assess managed agents, Bedrock/AgentCore, task AI APIs, SageMaker AI and Trainium/Inferentia against workload goals",
    "open-source": ("Verify license-included engines and operating systems (Oracle, SQL Server, Windows), SQL dialect, stored procedure and "
                    ".NET Framework compatibility, and the licensing terms that apply"),
    "cloud-native": "Verify deployment, observability, recovery procedures, team ownership and operational goals",
    "modern-devops": "Verify source, build, test and deployment history, IaC, artifact promotion, rollback, operational feedback and delivery metrics",
}


def option_gates(item, base_readiness):
    extra = item.get("additional_readiness", [])
    if item["category"] == "ai-accelerator":
        extra = ["model_compatibility", "benchmark"]
    return (*base_readiness, *extra)


def bootstrap_options(catalog, base_readiness, alternative):
    return [{
        "option_id": key, "selected": False, "status": "not_assessed",
        "use_case": "Unverified", "rationale": item["question"],
        "resource_ids": [], "finding_ids": [], "prerequisites": [item["question"]],
        "blockers": [], "validation": [], "alternatives": [alternative],
        "risks": [], "cost_assumptions": ["Measured workload and current rates are unverified"],
        "readiness": {gate: "unknown" for gate in option_gates(item, base_readiness)},
        "source_urls": [item["source"]], "source_checked_at": None,
    } for key, item in catalog.items()]


def bootstrap_ai_options():
    return bootstrap_options(AI_OPTIONS, AI_READINESS, "Compare retaining the current solution and non-AI alternatives")


def bootstrap_platform_options(pathway):
    catalog = {o["id"]: o for o in PLATFORM_GROUPS[pathway]["options"]}
    alternative = ("Compare retaining or improving existing CloudWatch and observability tools" if pathway == "modern-devops"
                   else "Compare existing tools, synchronous processing and simpler configuration improvements")
    return bootstrap_options(catalog, PLATFORM_READINESS, alternative)


def bootstrap():
    candidates = [{
        "pathway": key, "status": "not_assessed", "resource_ids": [], "finding_ids": [],
        "problem": "Link business and technical constraints to assessment findings", "rationale": QUESTIONS[key],
        "target_services": [], "alternatives": ["Compare improving or retaining the current solution"],
        "prerequisites": [QUESTIONS[key]], "blockers": [],
        "criteria": {name: {"score": None, "rationale": "Evidence unverified"} for name in WEIGHTS},
        "validation": [QUESTIONS[key]], "rollback": [], "wave": None, "owner": "Undecided",
        "cost_assumptions": ["Cost and savings have not been estimated"], "source_urls": [PUBLIC_SOURCES[key]],
    } for key in PATHWAYS]
    next(c for c in candidates if c["pathway"] == "genai")["ai_options"] = bootstrap_ai_options()
    for c in candidates:
        if c["pathway"] in PLATFORM_GROUPS:
            c[PLATFORM_GROUPS[c["pathway"]]["field"]] = bootstrap_platform_options(c["pathway"])
    return candidates


LICENSE_RULE = "COMMERCIAL_LICENSE_DEPENDENCY"
# Software a host's UserData names, for saying what an EC2 instance runs. Only names that appear in the script are reported;
# nothing is inferred from them.
HOST_SOFTWARE = ("kubectl", "helm", "docker", "containerd", "nginx", "httpd", "tomcat", "haproxy", "redis", "memcached", "rabbitmq", "kafka",
                 "zookeeper", "elasticsearch", "opensearch", "solr", "mysql", "mariadb", "postgresql", "mongodb", "cassandra", "jenkins", "airflow",
                 "openvpn", "squid", "wordpress", "iis", "java", "python", "nodejs", "dotnet")
SYSTEMD_UNIT = re.compile(r"systemctl\s+(?:--now\s+)?enable\s+(?:--now\s+)?([A-Za-z0-9_@.-]+)")
# A recommendation that hands a self-managed component to an AWS managed service: a move verb followed, in the same
# sentence, by "to/onto/into/with/on" and the managed service (the Managed services rationale lists those that concern a
# component on an EC2 instance). Databases stay with Managed database.
MANAGED_TARGETS = ("Fargate", "Lambda", "App Runner", "Systems Manager", "Session Manager", "ElastiCache", "MemoryDB", "Amazon MQ", "MSK",
                   "OpenSearch Service", "EKS Auto Mode", "AWS Batch", "Step Functions", "EventBridge Scheduler", "Managed Workflows")
MANAGED_MOVE = re.compile(r"(?i)\b(?:mov\w*|migrat\w*|replac\w*|consolidat\w*|hand\w*|shift\w*|repackag\w*|packag\w*)\b[^.]{0,90}?"
                          r"\b(?:to|onto|into|with|on|as)\s+(?:an?\s+|the\s+)?(?:AWS\s+|Amazon\s+)?(?:" + "|".join(re.escape(t) for t in MANAGED_TARGETS) + r")\b")
NOT_REVIEWED = "Not reviewed by a Specialist SA Agent in this run."


def tag_map(attributes):
    return {t.get("Key"): t.get("Value") for t in (attributes or {}).get("Tags") or [] if isinstance(t, dict)}


def is_eks_node(attributes):
    """Same test as the review rules: a worker node of an EKS node group or Karpenter."""
    tags = tag_map(attributes)
    return any(k in tags for k in ("aws:eks:cluster-name", "eks:cluster-name", "karpenter.sh/nodepool")) or any(
        str(k).startswith("kubernetes.io/cluster/") for k in tags)


def collected_user_data(run_dir, snapshot_dir=None):
    """{instance id: decoded UserData} from the describe-instance-attribute records stage 3 collected (03-supplementary, or a
    snapshot's own supplementary folder, the first that exists, as benefits.py reads them)."""
    out = {}
    folders = [Path(run_dir) / "03-supplementary"] if run_dir else []
    if snapshot_dir:
        folders.append(Path(snapshot_dir) / "supplementary")
    for folder in folders:
        manifest = folder / "supplementary-manifest.json"
        if not manifest.is_file():
            continue
        try:
            records = json.loads(manifest.read_text(encoding="utf-8")).get("records", [])
        except (OSError, ValueError):
            records = []
        for record in records:
            if record.get("operation") != "describe-instance-attribute" or record.get("status") != "ok":
                continue
            try:
                data = json.loads((folder / str(record.get("raw_path") or "")).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            user = data.get("UserData")
            decoded = user.get("Decoded") if isinstance(user, dict) else None
            if data.get("InstanceId") and decoded:
                out[data["InstanceId"]] = str(decoded)
        break
    return out


def evidence_context(inventory, checks, scope_ids=None, run_dir=None, snapshot_dir=None, specialists=None):
    """What the rationale of an inserted pathway may cite: in-scope resources, findings, collected UserData and the
    specialist reviews (05-modernization/specialist-advice.json entries)."""
    resources = [r for r in (inventory or {}).get("resources", []) if r.get("level") != "account" and (scope_ids is None or r.get("id") in scope_ids)]
    return {"resources": resources, "checks": list(checks or []), "user_data": collected_user_data(run_dir, snapshot_dir),
            "specialists": list(specialists or [])}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _first_sentence(text, limit=240):
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    return out if len(out) <= limit else ""


def host_software(user_data):
    """What a UserData script says the host runs: the names from HOST_SOFTWARE it mentions and the systemd units it enables."""
    low = user_data.lower()
    names = [w for w in HOST_SOFTWARE if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low)]
    units = list(dict.fromkeys(SYSTEMD_UNIT.findall(user_data)))
    parts = []
    if names:
        parts.append("UserData names " + _join(names[:4]))
    if units:
        parts.append(("enables the systemd unit " if len(units) == 1 else "enables the systemd units ") + _join(units[:3]))
    return " and ".join(parts)


def open_source_rationale(ctx):
    """(rationale, resource ids, finding ids) for Open source: the license-included findings, or what was looked at."""
    found = [c for c in ctx["checks"] if c.get("rule_id") == LICENSE_RULE and c.get("status") in {"FAIL", "WARN"}]
    known = {r["id"] for r in ctx["resources"]}
    if found:
        listed = "; ".join(f"{str(c.get('observation') or '').strip().rstrip('.')} ({c['id']})" for c in found[:4])
        more = f"; +{len(found) - 4} more" if len(found) > 4 else ""
        text = f"{_plural(len(found), 'license-included engine or operating system')} found in scope: {listed}{more}."
        return text, sorted({c["resource_id"] for c in found if c.get("resource_id") in known}), [c["id"] for c in found]
    dbs = [r for r in ctx["resources"] if r.get("type") == "rds.instance"]
    hosts = [r for r in ctx["resources"] if r.get("type") == "ec2.instance"]
    if not dbs and not hosts:
        return "No database instance or EC2 instance is in scope, so no license-included engine or operating system was found.", [], []
    looked = []
    if dbs:
        engines = sorted({str((r.get("attributes") or {}).get("Engine") or "engine not recorded") for r in dbs})
        looked.append(f"{_plural(len(dbs), 'RDS instance')} ({', '.join(engines)})")
    if hosts:
        platforms = sorted({str((r.get("attributes") or {}).get("PlatformDetails") or (r.get("attributes") or {}).get("Platform") or "platform not recorded") for r in hosts})
        looked.append(f"{_plural(len(hosts), 'EC2 instance')} ({', '.join(platforms)})")
    return ("No license-included engine or operating system was found among the in-scope databases and instances: " + _join(looked) + "."), [], []


def managed_services_rationale(ctx):
    """(rationale, resource ids, finding ids) for Managed services: the EC2 instances outside EKS node groups with what the
    evidence says they run, and the specialist recommendations that hand one of their components to a managed service."""
    instances = [r for r in ctx["resources"] if r.get("type") == "ec2.instance"]
    hosts = [r for r in instances if not is_eks_node(r.get("attributes"))]
    nodes = len(instances) - len(hosts)
    if not hosts:
        text = "No in-scope EC2 instance runs outside the EKS node groups, so no self-managed host software was found to hand to a managed service."
        if nodes:
            text = text[:-1] + f" ({_plural(nodes, 'EKS worker node')} left out)."
        return text, [], []
    names, described, quiet = [], [], []
    for r in hosts:
        a = r.get("attributes") or {}
        tags = tag_map(a)
        native = str(r.get("native_id") or a.get("InstanceId") or r["id"].rsplit(":", 1)[-1])
        bits = [str(a.get("InstanceType") or "type not recorded")]
        if tags.get("Role"):
            bits.append(f"Role tag {tags['Role']}")
        runs = host_software(ctx["user_data"].get(native) or ctx["user_data"].get(str(a.get("InstanceId") or "")) or "")
        if runs:
            bits.append(runs)
        else:
            quiet.append(r)
        names.append(tags.get("Name") or native)
        described.append(f"{tags.get('Name') or native}: {', '.join(bits)}")
    verb = "runs" if len(hosts) == 1 else "run"
    sentences = [f"{_plural(len(hosts), 'in-scope EC2 instance')} {verb} outside the EKS node groups: {_join(names)}.",
                 "; ".join(described) + "."]
    if nodes:
        sentences.append(f"{_plural(nodes, 'EKS worker node')} {'is' if nodes == 1 else 'are'} left out.")
    # What a specialist saw on a host whose UserData says nothing about what it runs.
    for r in quiet:
        native = str(r.get("native_id") or "")
        for sp in ctx["specialists"]:
            item = next((i for i in sp.get("current_state") or [] if r["id"] in (i.get("refs") or []) or (native and native in str(i.get("text") or ""))), None)
            sentence = _first_sentence(item.get("text")) if item else ""
            if sentence:
                sentences.append(f"{sp.get('role', sp.get('specialist'))}: {sentence}")
                break
    ids = {r["id"] for r in instances}
    natives = {str(r.get("native_id") or "") for r in instances} - {""}
    by_id = {c["id"]: c for c in ctx["checks"]}
    moves = []
    for sp in ctx["specialists"]:
        for rec in sp.get("recommendations") or []:
            refs = set(rec.get("refs") or [])
            touches = refs & ids or any((by_id.get(f) or {}).get("resource_id") in ids for f in refs) or any(n in str(rec.get("change") or "") for n in natives)
            if touches and (MANAGED_MOVE.search(str(rec.get("title") or "")) or MANAGED_MOVE.search(str(rec.get("change") or ""))):
                moves.append(f"\u201c{rec.get('title')}\u201d ({sp.get('role', sp.get('specialist'))})")
    if moves:
        sentences.append("Specialist recommendations that hand a component on these instances to a managed service: "
                         + "; ".join(moves[:3]) + (f"; +{len(moves) - 3} more" if len(moves) > 3 else "") + ".")
    return " ".join(sentences), sorted(r["id"] for r in hosts), []


RATIONALE = {"open-source": open_source_rationale, "managed-services": managed_services_rationale}


def inserted_pathway(key, ctx=None):
    """A pathway missing from an older candidates file, as not assessed. With an evidence context the rationale states what
    the evidence shows (and that no specialist reviewed it in this run); without one it keeps the open question."""
    c = next(x for x in bootstrap() if x["pathway"] == key)
    if ctx is not None and key in RATIONALE:
        text, resource_ids, finding_ids = RATIONALE[key](ctx)
        c.update(rationale=f"{text} {NOT_REVIEWED}", resource_ids=resource_ids, finding_ids=finding_ids)
    return c


def normalize_candidates(candidates, evidence=None):
    """Accept the legacy catalogs without modifying supplied assessments: the six pathways before Modern DevOps, and the
    seven before Managed services and Open source. Missing pathways are appended as not assessed."""
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        raise ValueError("candidates must be an array of objects")
    keys = [c.get("pathway") for c in candidates]
    if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("invalid or duplicate pathway")
    unknown = set(keys) - set(PATHWAYS)
    missing = [k for k in PATHWAYS if k not in keys]
    if unknown or set(missing) - {"modern-devops", *ADDED_PATHWAYS}:
        raise ValueError(f"all {len(PATHWAYS)} pathways required; only a legacy catalog without Modern DevOps, Managed Services "
                         "or Open Source can be upgraded")
    added = [inserted_pathway(k, evidence) if k in ADDED_PATHWAYS else next(c for c in bootstrap() if c["pathway"] == k) for k in missing]
    normalized = [dict(c) for c in [*candidates, *added]]
    for c in normalized:
        if c["pathway"] == "genai" and "ai_options" not in c:
            c["ai_options"] = bootstrap_ai_options()
        if c["pathway"] in PLATFORM_GROUPS:
            field = PLATFORM_GROUPS[c["pathway"]]["field"]
            if field not in c:
                c[field] = bootstrap_platform_options(c["pathway"])
    return normalized


def ordered(candidates):
    """Candidates in report order: the seven directions, then the foundations."""
    order = {k: i for i, k in enumerate((*DIRECTIONS, *FOUNDATIONS))}
    return sorted(candidates, key=lambda c: order.get(c.get("pathway"), len(order)))


def validate_ai_options(options, resource_ids, finding_ids):
    return validate_options(options, resource_ids, finding_ids, AI_OPTIONS, AI_READINESS)


def validate_options(options, resource_ids, finding_ids, catalog, base_readiness):
    if not isinstance(options, list) or any(not isinstance(o, dict) for o in options):
        raise ValueError("service options must be an array of objects")
    keys = [o.get("option_id") for o in options]
    if (any(not isinstance(k, str) for k in keys) or len(keys) != len(catalog)
            or set(keys) != set(catalog)):
        raise ValueError("service options must contain each catalog ID exactly once")
    checked = []
    for option in options:
        o = dict(option)
        key = o["option_id"]
        if not isinstance(o.get("selected"), bool) or o.get("status") not in STATUSES:
            raise ValueError("invalid service selection or status")
        for field in ("use_case", "rationale"):
            if not isinstance(o.get(field), str) or not o[field].strip():
                raise ValueError(f"service {field} is required")
        for field in ("resource_ids", "finding_ids", "prerequisites", "blockers", "validation",
                      "alternatives", "risks", "cost_assumptions", "source_urls"):
            if not isinstance(o.get(field), list) or any(not isinstance(v, str) or not v.strip() for v in o[field]):
                raise ValueError(f"service {field} must be a string list")
        if any(r not in resource_ids for r in o["resource_ids"]) or any(f not in finding_ids for f in o["finding_ids"]):
            raise ValueError("service option references unknown resource or finding")
        required = set(option_gates(catalog[key], base_readiness))
        readiness = o.get("readiness")
        if (not isinstance(readiness, dict) or set(readiness) != required
                or any(not isinstance(v, str) or v not in AI_READINESS_STATES for v in readiness.values())):
            raise ValueError("service readiness gates missing or invalid")
        reviewed = o.get("source_checked_at")
        if reviewed is not None:
            try:
                reviewed = datetime.strptime(reviewed, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise ValueError("service source_checked_at must be YYYY-MM-DD or null")
            if reviewed > datetime.now(timezone.utc).date():
                raise ValueError("service source check cannot be in the future")
        if o["selected"] and o["status"] not in {"candidate", "conditional"}:
            raise ValueError("selected service option must be candidate or conditional")
        if o["status"] in {"candidate", "conditional"} and (
                not o["resource_ids"] or not o["finding_ids"] or not o["validation"] or not o["alternatives"]):
            raise ValueError("service candidate needs evidence, validation and alternatives")
        ready = (o["status"] == "candidate" and not o["blockers"] and reviewed is not None
                 and bool(o["source_urls"]) and bool(o["cost_assumptions"])
                 and all(v == "confirmed" for v in readiness.values()))
        if o["status"] == "candidate" and not ready:
            raise ValueError("service candidate requires confirmed gates, checked sources and no blockers; use conditional")
        o["adoption_ready"] = ready
        checked.append(o)
    return checked


def rank(candidates, resource_ids, finding_ids):
    candidates = normalize_candidates(candidates)
    result = []
    for candidate in candidates:
        c = dict(candidate)
        if c["status"] not in STATUSES:
            raise ValueError("invalid pathway status")
        if any(r not in resource_ids for r in c["resource_ids"]):
            raise ValueError("candidate references unknown resource")
        if any(f not in finding_ids for f in c["finding_ids"]):
            raise ValueError("candidate references unknown finding")
        for field in ("problem", "rationale", "owner"):
            if not isinstance(c.get(field), str) or not c[field].strip():
                raise ValueError(f"candidate {field} is required")
        for field in ("target_services", "alternatives", "prerequisites", "blockers", "validation", "rollback", "cost_assumptions", "source_urls"):
            if not isinstance(c.get(field), list) or any(not isinstance(v, str) for v in c[field]):
                raise ValueError(f"candidate {field} must be a string list")
        if set(c["criteria"]) != set(WEIGHTS):
            raise ValueError("all six criteria required")
        total, known = 0.0, 0
        for name, weight in WEIGHTS.items():
            criterion = c["criteria"][name]
            value = criterion["score"]
            if not isinstance(criterion.get("rationale"), str) or not criterion["rationale"].strip():
                raise ValueError("criterion rationale required")
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 5:
                    raise ValueError("criterion score must be 1..5 or null")
                total += weight * value / 5
                known += weight
        if c["status"] in {"candidate", "conditional"} and (not c["finding_ids"] or not c["resource_ids"]):
            raise ValueError("candidate/conditional recommendation must link findings and resources")
        if c["status"] == "candidate" and (not c["validation"] or not c["rollback"] or not c["target_services"]):
            raise ValueError("candidate requires target, validation and rollback")
        c.update(score=round(total, 1) if known == 100 else None, known_weight_percent=known,
                 rank_eligible=c["status"] == "candidate" and not c["blockers"] and known == 100)
        if c["pathway"] == "genai":
            c["ai_options"] = validate_ai_options(c["ai_options"], resource_ids, finding_ids)
            selected = [o for o in c["ai_options"] if o["selected"]]
            c["rank_eligible"] = c["rank_eligible"] and bool(selected) and all(o["adoption_ready"] for o in selected)
        if c["pathway"] in PLATFORM_GROUPS:
            group = PLATFORM_GROUPS[c["pathway"]]
            field = group["field"]
            catalog = {o["id"]: o for o in group["options"]}
            c[field] = validate_options(c[field], resource_ids, finding_ids, catalog, PLATFORM_READINESS)
            # These are optional implementations of broader pathways: CI/CD or
            # service-boundary improvements do not require buying these services.
            selected = [o for o in c[field] if o["selected"]]
            c["rank_eligible"] = c["rank_eligible"] and all(o["adoption_ready"] for o in selected)
        result.append(c)
    eligible = sorted((c for c in result if c["rank_eligible"]), key=lambda c: (-c["score"], c["pathway"]))
    positions = {c["pathway"]: i+1 for i, c in enumerate(eligible)}
    for c in result:
        c["rank"] = positions.get(c["pathway"])
    return result


def ai_option_reports(options):
    return option_reports(options, AI_OPTIONS, "GenAI/AI service suitability")


def evidenced(option):
    """An option deserves a full write-up only when it is selected, judged, or tied to resources or findings."""
    return bool(option["selected"] or option["status"] != "not_assessed" or option["resource_ids"] or option["finding_ids"])


def option_reports(options, catalog, title):
    reviewed = [o for o in options if not evidenced(o)]
    options = [o for o in options if evidenced(o)]
    lines = ["", f"### {title}", "",
             "Assessments are based on supplied evidence, not automatic suitability inference. Unselected options are not part of the proposed combination.", "",
             "| Option | Category | Assessment | Selected | Unverified/blocked criteria |", "|---|---|---|---|---|"]
    rows, details = [], []
    for o in options:
        item = catalog[o["option_id"]]
        unresolved = ", ".join(AI_READINESS_LABELS[k] for k, v in o["readiness"].items() if v != "confirmed") or "None"
        selected = "Yes" if o["selected"] else "No"
        status = AI_STATUS_LABELS[o["status"]]
        lines.append(f"| {item['name']} | {AI_CATEGORY_LABELS[item['category']]} | {status} | {selected} | {unresolved} |")
        rows.append(f"<tr><td>{item['name']}</td><td>{status}</td><td>{selected}</td><td>{html.escape(unresolved)}</td></tr>")
        pairs = [
            ("Workload use case", o["use_case"]), ("Assessment rationale", o["rationale"]),
            ("Target", ", ".join(o["resource_ids"]) or "Unverified"), ("Findings", ", ".join(o["finding_ids"]) or "Unverified"),
            ("Prerequisites", "; ".join(o["prerequisites"])), ("Blocker", "; ".join(o["blockers"]) or "None identified"),
            ("Validation", "; ".join(o["validation"]) or "Unverified"), ("Alternatives", "; ".join(o["alternatives"])),
            ("Risks/responsibilities", "; ".join(o["risks"]) or "Unverified"), ("Cost assumptions", "; ".join(o["cost_assumptions"])),
            ("Source review date", o["source_checked_at"] or "Unverified"), ("Sources", ", ".join(o["source_urls"])),
        ]
        details.append(f"<section><h4>{item['name']}</h4>" + "".join(
            f"<p><strong>{label}:</strong> {html.escape(value)}</p>" for label, value in pairs) + "</section>")
    for o in options:
        item = catalog[o["option_id"]]
        lines += ["", f"#### {item['name']}", "", f"- Workload use case: {o['use_case']}", f"- Assessment rationale: {o['rationale']}"]
        for label, field in [("Target","resource_ids"),("Findings","finding_ids"),("Prerequisites","prerequisites"),
                             ("Blocker","blockers"),("Validation","validation"),("Alternatives","alternatives"),
                             ("Risks/responsibilities","risks"),("Cost assumptions","cost_assumptions"),("Sources","source_urls")]:
            lines.append(f"- {label}: {'; '.join(o[field]) or 'Unverified or none identified'}")
        lines.append(f"- Source review date: {o['source_checked_at'] or 'Unverified'}")
    if not options:
        lines += ["", "No option in this group has evidence of need in this workload."]
    if reviewed:
        lines += ["", "Reviewed without evidence of need: " + "; ".join(f"{catalog[o['option_id']]['name']} ({o['rationale']})" for o in reviewed)]
    reviewed_html = ("<p class=\"small\"><strong>Reviewed without evidence of need:</strong> " + "; ".join(
        f"{html.escape(catalog[o['option_id']]['name'])} ({html.escape(o['rationale'])})" for o in reviewed) + "</p>") if reviewed else ""
    table = ('<table class="option-table"><colgroup><col style="width:26%"><col style="width:12%"><col style="width:8%"><col style="width:54%"></colgroup><tr><th>Option</th><th>Assessment</th><th>Selected</th><th>Unverified/blocked criteria</th></tr>'
             + "".join(rows) + "</table>") if rows else "<p>No option in this group has evidence of need in this workload.</p>"
    markup = (f"<h3>{html.escape(title)}</h3><p>Assessments are based on supplied evidence, not automatic suitability inference. Unselected options are not included in the proposed architecture.</p>"
              + table + "".join(details) + reviewed_html)
    return lines, markup


def candidate_option_reports(candidate):
    pathway = candidate["pathway"]
    if pathway == "genai":
        return ai_option_reports(candidate.get("ai_options", bootstrap_ai_options()))
    if pathway in PLATFORM_GROUPS:
        group = PLATFORM_GROUPS[pathway]
        options = candidate.get(group["field"], bootstrap_platform_options(pathway))
        return option_reports(options, {o["id"]: o for o in group["options"]}, group["title"])
    return [], ""


def diagram_image(path):
    raw = path.read_bytes()
    report_kit.scan_svg(raw)
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode()


def reports(output, data, diagram, run_meta=None):
    candidates = ordered(data["candidates"])
    status = "Includes conditional or unassessed options" if any(c["status"] != "candidate" for c in candidates) else "Candidate comparison"
    lines = ["# AWS Modernization Pathway recommendations", "", f"- Workload: {data['workload']}",
             f"- Status: {status}", "- Scores are local comparison measures, not official AWS scores.",
             "- Candidates and conditions are design proposals; no resources have been created or migrated.", "",
             "| Pathway | Assessment | Suitability | Verified criteria weight % | Confirmed candidate rank |",
             "|---|---|---:|---:|---:|"]
    for c in candidates:
        lines.append(f"| {PATHWAYS[c['pathway']]} | {c['status']} | {c['score'] if c['score'] is not None else 'Unassessed'} | {c['known_weight_percent']} | {c['rank'] or '—'} |")
    for note in data.get("input_notes", []):
        lines += ["", note]
    lines += ["", "## Proposed architecture", ""]
    image_markup = "<p>TO-BE: the proposed-architecture diagram is required</p>"
    if diagram:
        relative = Path(os.path.relpath(diagram, output)).as_posix()
        lines += [f"![TO-BE architecture with conditions and proposal states]({relative})"]
        image_markup = f'<img alt="TO-BE architecture with conditions and proposal states" src="{diagram_image(diagram)}">'
    else:
        lines += ["Draft pending the proposed-architecture diagram (diagram.py --graph)."]
    for c in candidates:
        lines += ["", f"## {PATHWAYS[c['pathway']]}", "", f"- Assessment/rationale: {c['status']} — {c['rationale']}",
                  f"- Current problem: {c['problem']}", f"- Target: {', '.join(c['resource_ids']) or 'Unverified'}",
                  f"- Related findings: {', '.join(c['finding_ids']) or 'Evidence linkage required'}",
                  f"- Target services: {', '.join(c['target_services']) or 'Not selected'}",
                  f"- Alternatives: {'; '.join(c['alternatives'])}", f"- Prerequisites: {'; '.join(c['prerequisites'])}",
                  f"- Blocker: {'; '.join(c['blockers']) or 'None identified - interpret alongside verification status'}",
                  f"- Validation: {'; '.join(c['validation'])}", f"- Rollback: {'; '.join(c['rollback']) or 'Design required'}",
                  f"- Wave / Owner: {c['wave'] if c['wave'] is not None else 'Undecided'} / {c['owner']}",
                  f"- Cost assumptions: {'; '.join(c['cost_assumptions'])}", f"- Sources: {', '.join(c['source_urls'])}"]
        lines += candidate_option_reports(c)[0]
    lines += [""] + report_kit.run_metadata_markdown(run_meta)
    lines += ["", "## Shared prerequisites and deferred decisions", "",
              "Link assessment security, observability and recovery tasks to remediation; use unresolved candidate criteria in the next validation plan.",
              "Configuration snapshots alone do not establish application compatibility, business value, migration duration or cost savings.", ""]
    (output / "recommendations.md").write_text("\n".join(lines), encoding="utf-8")
    page = render_html(data, status, diagram, image_markup, run_meta)
    (output / "recommendations.html").write_text(page, encoding="utf-8")


def wave_rows(candidates):
    """Ordinal roadmap rows derived from candidate waves; no calendar dates are implied."""
    waves = {}
    for c in candidates:
        if c.get("wave") is not None and c["status"] in {"candidate", "conditional"}:
            waves.setdefault(str(c["wave"]), []).append(PATHWAYS[c["pathway"]].replace("Move to ", ""))
    ordered = sorted(waves, key=lambda w: (len(w), w))
    return [(wave, index, index, ", ".join(waves[wave])) for index, wave in enumerate(ordered)]


def render_html(data, status, diagram, image_markup, run_meta=None):
    """Visual HTML report: KPI tiles, verified-weight chart, pathway cards, criteria heat map, option grids and roadmap."""
    kit = report_kit
    candidates = ordered(data["candidates"])
    counts = {s: sum(1 for c in candidates if c["status"] == s) for s in kit.PATHWAY_STATUS}
    ranked = [c for c in candidates if c.get("rank")]
    genai = next((c for c in candidates if c["pathway"] == "genai"), None)
    ai_options = genai.get("ai_options", []) if genai else []
    selected_ai = sum(1 for o in ai_options if o["selected"])
    criteria = {"business_value": "Business value 25", "technical_fit": "Technical fit 25", "operating_model": "Operating model 15",
                "risk_manageability": "Risk manageability 15", "delivery_feasibility": "Delivery feasibility 10", "cost_confidence": "Cost confidence 10"}

    def short(pathway):
        return PATHWAYS[pathway].replace("Move to ", "")

    def card(c):
        label, color = kit.PATHWAY_STATUS[c["status"]]
        services = "".join(f"<li>{kit.esc(s)}</li>" for s in c["target_services"][:4]) or "<li>Not selected</li>"
        wave = kit.chip(f"wave {c['wave']}", kit.INK) if c.get("wave") is not None else ""
        score = kit.num(c["score"]) if c.get("score") is not None else "unassessed"
        rank = f"rank {c['rank']}" if c.get("rank") else "unranked"
        return (f'<div class="pathcard" style="border-top-color:{color}"><h3><span>{kit.esc(PATHWAYS[c["pathway"]])}</span><span>{kit.pathway_chip(c["status"])}{wave}</span></h3>'
                f'<p>{kit.esc(c["problem"])}</p><div><b class="small">Target services</b><ul>{services}</ul></div>'
                f'<p class="small" style="margin-top:auto">Findings <code>{kit.esc(", ".join(c["finding_ids"]) or "-")}</code> · verified weight {c["known_weight_percent"]}% · score {kit.esc(score)} · {rank}</p></div>')

    heat = ('<table><thead><tr><th>Pathway</th>' + "".join(f'<th style="text-align:center">{kit.esc(v)}</th>' for v in criteria.values())
            + '<th style="text-align:center">Verified weight</th><th style="text-align:center">Score / rank</th></tr></thead><tbody>')
    for c in candidates:
        score = kit.num(c["score"]) if c.get("score") is not None else "-"
        heat += (f'<tr><td><b>{kit.esc(PATHWAYS[c["pathway"]])}</b> {kit.pathway_chip(c["status"])}</td>'
                 + "".join(kit.heat_cell(c["criteria"][k]["score"]) for k in criteria)
                 + f'<td style="text-align:center;font-family:{kit.MONO}">{c["known_weight_percent"]}%</td>'
                 f'<td style="text-align:center;font-family:{kit.MONO}">{kit.esc(score)} / {c["rank"] or "unranked"}</td></tr>')
    heat += "</tbody></table>"

    def option_grid(options, catalog):
        cells = []
        rest = [o for o in options if not evidenced(o)]
        for o in [o for o in options if evidenced(o)]:
            label, color = kit.PATHWAY_STATUS[o["status"]]
            selected = kit.chip("selected", kit.ACCENT) if o["selected"] else ""
            cells.append(f'<div style="border-top-color:{color}"><b>{kit.esc(catalog[o["option_id"]]["name"])}</b>{kit.pathway_chip(o["status"])} {selected}'
                         f'<div class="small" style="margin-top:6px"><b>Use case</b> {kit.esc(o["use_case"])}</div><div class="small">{kit.esc(o["rationale"])}</div></div>')
        tail = ('<p class="small"><b>Reviewed without evidence of need:</b> ' + "; ".join(kit.esc(catalog[o["option_id"]]["name"]) for o in rest) + "</p>") if rest else ""
        return ('<div class="opt">' + "".join(cells) + "</div>" if cells else "") + tail

    option_sections = []
    if genai:
        option_sections.append(f"<h3>GenAI/AI service options ({selected_ai} of {len(ai_options)} selected)</h3>{option_grid(ai_options, AI_OPTIONS)}")
    for pathway, group in PLATFORM_GROUPS.items():
        c = next((x for x in candidates if x["pathway"] == pathway), None)
        if c and group["field"] in c:
            option_sections.append(f"<h3>{kit.esc(group['title'])}</h3>{option_grid(c[group['field']], {o['id']: o for o in group['options']})}")

    def detail(c):
        rows = [("Rationale", c["rationale"]), ("Current problem", c["problem"]), ("Target", ", ".join(c["resource_ids"]) or "Unverified"),
                ("Related findings", ", ".join(c["finding_ids"]) or "Evidence linkage required"), ("Target services", ", ".join(c["target_services"]) or "Not selected"),
                ("Alternatives", "; ".join(c["alternatives"])), ("Prerequisites", "; ".join(c["prerequisites"])),
                ("Blocker", "; ".join(c["blockers"]) or "None identified - interpret alongside verification status"),
                ("Validation", "; ".join(c["validation"]) or "Unverified"), ("Rollback", "; ".join(c["rollback"]) or "Design required"),
                ("Wave / owner", f"{c['wave'] if c['wave'] is not None else 'Undecided'} / {c['owner']}"), ("Cost assumptions", "; ".join(c["cost_assumptions"]))]
        table = "".join(f'<tr><th style="width:160px">{kit.esc(a)}</th><td>{kit.esc(b)}</td></tr>' for a, b in rows)
        sources = " · ".join(f'<a href="{kit.esc(u)}" target="_blank" rel="noopener">{kit.esc(u)}</a>' for u in c["source_urls"])
        criteria_rows = "".join(f'<tr><th style="width:160px">{kit.esc(criteria[k])}</th><td>{kit.esc(kit.num(c["criteria"][k]["score"]) if c["criteria"][k]["score"] is not None else "unknown")} - {kit.esc(c["criteria"][k]["rationale"])}</td></tr>' for k in criteria)
        return (f'<details><summary>{kit.esc(PATHWAYS[c["pathway"]])} {kit.pathway_chip(c["status"])}<span class="small">verified weight {c["known_weight_percent"]}%</span></summary>'
                f'<div class="body"><table><tbody>{table}<tr><th>Sources</th><td>{sources}</td></tr></tbody></table><h4>Criteria</h4><table><tbody>{criteria_rows}</tbody></table>'
                + candidate_option_reports(c)[1] + "</div></details>")

    roadmap = wave_rows(candidates)
    roadmap_html = (f'<div class="card">{kit.gantt(roadmap, focal=roadmap[0][0] if roadmap else None)}</div>'
                    '<p class="small">Bars are ordinal: each wave starts after the previous wave meets its acceptance criteria. No calendar dates are implied.</p>'
                    if roadmap else '<div class="callout"><p>No wave has been assigned to a candidate or conditional pathway yet.</p></div>')
    weights = [(short(c["pathway"]), c["known_weight_percent"]) for c in candidates]
    notes = "".join(f'<div class="callout"><p>{kit.esc(n)}</p></div>' for n in data.get("input_notes", []))
    diagram_block = (f'<figure>{image_markup}<figcaption>TO-BE architecture · retained, proposed and unresolved components are distinguished in the diagram legend</figcaption></figure>'
                     if diagram else f'<div class="callout">{image_markup} <p>Draw the proposed architecture with diagram.py --graph before delivery.</p></div>')
    meta_chip = (f"model {run_meta['models_seen'][0]}" if run_meta and len(run_meta["models_seen"]) == 1 else ("model not recorded" if not run_meta else f"{len(run_meta['models_seen'])} models"))
    hero = kit.hero(f"AWS modernization pathways · {len(DIRECTIONS)} directions and {len(FOUNDATIONS)} foundations · 12 AI options · 7 platform options", "AWS Modernization Pathway recommendations", status,
                    [f"account {data['account_id']}", f"workload {data['workload']}", f"generated {data['generated_at'][:16]}",
                     f"catalog v{data['pathway_catalog_version']}", f"diagram {data.get('diagram', 'pending')}", meta_chip])
    nav = kit.toc([("summary", "Summary"), ("pathways", "Directions and foundations"), ("architecture", "Proposed architecture"), ("options", "AI · observability · messaging"),
                   ("roadmap", "Roadmap"), ("details", "Pathway details"), ("provenance", "Run provenance"), ("deferred", "Deferred decisions")])
    kpis = (kit.kpi(len(candidates), "pathways assessed") + kit.kpi(counts["candidate"], "candidate", "pass") + kit.kpi(counts["conditional"], "conditional", "warn")
            + kit.kpi(counts["not_recommended"], "not recommended", "unknown") + kit.kpi(counts["not_assessed"], "not assessed", "unknown")
            + kit.kpi(len(ranked), "ranked", "fail" if not ranked else "pass") + kit.kpi(f"{selected_ai}/{len(ai_options)}" if ai_options else "-", "AI options selected", "link")
            + kit.kpi(len(roadmap) or "-", "roadmap waves", "accent"))
    rank_note = ("Ranking requires candidate status, no blockers and all six criteria verified; selected AI, observability and messaging options must also be adoption-ready."
                 if not ranked else "Ranked pathways: " + ", ".join(f"{c['rank']}. {short(c['pathway'])} ({kit.num(c['score'])})" for c in sorted(ranked, key=lambda c: c["rank"])))
    body = f"""{hero}{nav}<div class="wrap">
<section id="summary"><h2>Summary</h2><p class="lead">Scores are local comparison measures, not official AWS scores. Unknown criteria stay null instead of becoming zero, and candidates are design proposals: no resources have been created or migrated.</p>
<div class="kpis">{kpis}</div>{notes}
<div class="grid g2"><div class="card"><h3>Verified criteria weight per pathway</h3><p class="sub">share of the six weighted criteria with a known score · 100% is required for a total score and rank</p>{kit.count_bars(weights, "verified-weight", "Verified criteria weight per pathway", "Horizontal bars show the percentage of criteria weight with verified scores for each pathway.", suffix="%")}</div>
<div class="card"><h3>Ranking status</h3><p class="sub">{len(ranked)} ranked · {counts["candidate"]} candidate · {counts["conditional"]} conditional</p><p class="small">{kit.esc(rank_note)}</p></div></div></section>
<section id="pathways"><h2>Seven directions and two foundations</h2><p class="lead">The seven modernization directions come first; Modern DevOps and Cloud Native are the foundations, the delivery and operations practices that make the moves safe. Status colours: candidate (green), conditional (orange), not recommended (grey), not assessed (light grey). Target services are proposals, not confirmed designs.</p>
<div class="grid g3">{"".join(card(c) for c in candidates)}</div>
<h3>Criteria heat map (1-5, "-" = unknown)</h3>{heat}
<p class="small">Weighted fit = sum of weight x score / 5 over the six criteria; a total score and rank appear only when every criterion is known.</p></section>
<section id="architecture"><h2>Proposed architecture</h2><p class="lead">Retained, proposed and removal-candidate components must be distinguished; unverified relationships remain explicit.</p>{diagram_block}</section>
<section id="options"><h2>AI · observability · messaging fit</h2><p class="lead">Every catalog option records a reason; only selected options belong to the proposal and they must be adoption-ready before the pathway can rank.</p>{"".join(option_sections)}</section>
<section id="roadmap"><h2>Roadmap</h2><p class="lead">Waves follow evidence and dependencies with explicit acceptance, validation, rollback and ownership; see pathway details for each wave's conditions.</p>{roadmap_html}</section>
<section id="details"><h2>Pathway details</h2>{"".join(detail(c) for c in candidates)}</section>
{kit.run_metadata_html(run_meta)}
<section id="deferred"><h2>Shared prerequisites and deferred decisions</h2><ul><li>Link assessment security, observability and recovery tasks to remediation; use unresolved candidate criteria in the next validation plan.</li>
<li>Configuration snapshots alone do not establish application compatibility, business value, migration duration or cost savings.</li></ul></section>
<footer>pathways.json · recommendations.md · candidates.draft.json · pathway catalog v{kit.esc(data["pathway_catalog_version"])} · AI catalog v{kit.esc(data["ai_option_catalog_version"])} · platform catalog v{kit.esc(data["platform_option_catalog_version"])}</footer></div>"""
    return kit.page("AWS modernization recommendations", body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--findings", type=Path, required=True)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagram", type=Path)
    parser.add_argument("--run-metadata", type=Path, help="Agent-recorded run-metadata.json (model, inference time, tokens); see aws-mcp-setup run_metadata.py")
    args = parser.parse_args()
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        findings = json.loads(args.findings.read_text(encoding="utf-8"))
        for key in ("schema_version", "account_id", "run_id"):
            if inventory.get(key) != findings.get(key):
                raise ValueError(f"input {key} mismatch")
        if inventory.get("schema_version") != "1.0" or inventory.get("collection_status") == "failed":
            raise ValueError("unsupported or failed inventory")
        run_meta = report_kit.load_run_metadata(args.run_metadata)
        candidates = json.loads(args.candidates.read_text(encoding="utf-8")) if args.candidates else bootstrap()
        if args.candidates:
            import advice  # noqa: E402  (same folder)
            language = report_kit.english_errors(advice.candidate_texts(candidates, args.candidates.name))
            if language:
                raise ValueError(language[0] + f" ({len(language)} field(s))")
        scope_path = args.inventory.parent / "scope-resources.json"
        scope_ids = {x["id"] for x in json.loads(scope_path.read_text(encoding="utf-8")).get("resources", [])} if scope_path.is_file() else None
        advice_path = args.output / "specialist-advice.json"
        specialists = json.loads(advice_path.read_text(encoding="utf-8")).get("specialists", []) if advice_path.is_file() else []
        evidence = evidence_context(inventory, findings["checks"], scope_ids, args.output.parent, args.inventory.parent, specialists)
        normalized = normalize_candidates(candidates, evidence)
        given = {c.get("pathway") for c in candidates}
        notes = []
        if "modern-devops" not in given:
            notes.append("Preserved the legacy six pathways and added Move to Modern DevOps as unassessed. The original input file was not modified.")
        added = [PATHWAYS[k] for k in ADDED_PATHWAYS if k not in given]
        if added:
            notes.append(f"Added {' and '.join(added)} as not assessed, with reasons taken from the findings, the inventory and the specialist reviews. "
                         "The original input file was not modified.")
        if any(c["pathway"] == "genai" and "ai_options" not in c for c in candidates):
            notes.append("Added 12 unassessed AI options to the legacy GenAI input. GenAI remains unranked until selected options are verified.")
        if any(c["pathway"] in PLATFORM_GROUPS and PLATFORM_GROUPS[c["pathway"]]["field"] not in c for c in candidates):
            notes.append("Added unassessed Observability/Message-Event Driven options to the legacy input. Original input and ranking rules for unselected options are preserved.")
        candidates = normalized
        rated = rank(candidates, {r["id"] for r in inventory["resources"]}, {f["id"] for f in findings["checks"]})
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import next_steps  # noqa: E402
        steps = next_steps.select(rated, findings["checks"], inventory, scope_ids)
        if args.diagram:
            diagram_image(args.diagram)
        args.output.mkdir(parents=True, exist_ok=True)
        draft = args.output / "candidates.draft.json"
        if not args.candidates and not draft.exists():
            draft.write_text(json.dumps(candidates, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        result = {"schema_version": "1.0", "run_id": inventory["run_id"], "account_id": inventory["account_id"],
                  "workload": args.workload, "generated_at": datetime.now(timezone.utc).isoformat(),
                  "pathway_catalog_version": "3.0", "input_notes": notes,
                  "ai_option_catalog_version": AI_CATALOG["version"],
                  "platform_option_catalog_version": PLATFORM_CATALOG["version"],
                  "weights": WEIGHTS, "candidates": rated, "diagram": "included" if args.diagram else "pending", "run_metadata": run_meta,
                  "next_steps": steps, "next_steps_catalog_version": next_steps.CATALOG["verified_on"]}
        (args.output / "pathways.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        reports(args.output, result, args.diagram, run_meta)
        print(json.dumps({"status": "generated", "ranked_candidates": sum(c["rank_eligible"] for c in rated),
                          "unassessed": sum(c["score"] is None for c in rated), "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"Recommendation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
