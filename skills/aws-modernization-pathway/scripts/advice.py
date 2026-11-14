#!/usr/bin/env python3
"""Specialist SA Agent reviews for stage 5: briefs, validation and merge. No AWS API calls.

Five Specialist SA Agents (sub-agents that take the role of an AWS specialist SA, or the main agent in turn when the client
has no sub-agent tool) each look at
the workload through one domain and write advice with a focused architecture diagram:

  containers  Container & Serverless Specialist SA Agent   container, serverless, cloud-native (messaging options)
  database    Database Specialist SA Agent                 managed-database
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
    "containers": {"role": "Container & Serverless Specialist SA Agent", "pathways": ("container", "serverless", "cloud-native"),
                   "types": ("ecs.", "eks.", "ec2.instance", "autoscaling.", "elbv2.", "lambda.", "apigateway", "sqs.", "sns.", "events.", "stepfunctions.", "ecr.", "cloudfront."),
                   "rules": r"^(ECS_|EKS_|EC2_|ASG|ELB_|ALB_|LAMBDA|SQS|SNS|CONTAINER_|GRAVITON|COMPUTE_OPTIMIZER_FINDING|CUSTOMER_INGRESS|SUS_|PERF_)"},
    "database": {"role": "Database Specialist SA Agent", "pathways": ("managed-database",),
                 "types": ("rds.", "elasticache.", "dynamodb.", "secretsmanager.", "backup."),
                 "rules": r"^(RDS_|AURORA_|ELASTICACHE_|CACHE_|DB_|DYNAMODB|BACKUP_|SECRET_|STACK_DATA_|REL_RESTORE|DATA_SG_|SCOPE_DYNAMODB)"},
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


def brief_text(ctx, sid):
    spec = SPECIALISTS[sid]
    run = ctx.run
    folder = ctx.folder(sid)
    language = ctx.scope.get("output_language") or "en"
    profile = ((ctx.scope.get("auth") or {}).get("profile")) or "<profile>"
    resources = [r for r in ctx.inventory.get("resources", []) if r.get("level") != "account" and (ctx.scope_ids is None or r["id"] in ctx.scope_ids) and in_domain(spec, r)]
    pattern = re.compile(spec["rules"])
    domain_types = {r["id"] for r in resources}
    checks = [c for c in ctx.findings.get("checks", []) if c["status"] in {"FAIL", "WARN", "UNKNOWN"} and (pattern.search(c["rule_id"]) or c.get("resource_id") in domain_types)]
    review = SKILL_DIR.parent / "aws-architecture-review"
    diagram_skill = SKILL_DIR.parent / "aws-diagram-design"
    rows = "\n".join(f"| `{r['id']}` | {r['type']} | {r.get('name') or r.get('native_id') or ''} |" for r in resources[:80])
    more = f"\n\n({len(resources) - 80} more in the inventory.)" if len(resources) > 80 else ""
    frows = "\n".join(f"| {c['id']} | {c['rule_id']} | {c['status']} | {c.get('severity')} | {str(c.get('observation', ''))[:160].replace('|', '/')} |" for c in checks)
    pathways = ", ".join(f"`{p}`" for p in spec["pathways"])
    return f"""# Brief: {spec['role']} for {ctx.scope.get('workload_name') or ctx.inventory.get('workload') or 'the workload'}

Run `{run}` · account {ctx.inventory.get('account_id')} · {', '.join(ctx.inventory.get('regions') or [])} · output language **{language}**
(write every sentence, title, diagram label and caption in this language; service names, resource names and IDs stay as they are).

You cover the pathways {pathways}. Read `{SKILL_DIR}/references/specialists.md` (shared rules and the output schema) and
`{SKILL_DIR}/references/specialists/{sid}.md` (your domain) before anything else.

## Evidence

- Inventory the review judged: `{ctx.snapshot}/inventory.json` (in-scope IDs: `{ctx.snapshot}/scope-resources.json`)
- Findings: `{run}/04-review/findings.json` (all pillars; the ones below are the likely starting points, not a limit)
- Supplementary evidence (stack templates, IAM policy documents, task definitions, source files): `{run}/03-supplementary/`
- Current architecture graph: `{run}/04-review/diagrams/diagram-graph.json` (reuse its labels and resource_ids for retained components)
- Scope interview and context: `{run}/scope.json` · key list prices already looked up: `{run}/05-modernization/list-prices.json`
- Pathway draft to start from: `{run}/05-modernization/candidates.draft.json` (your pathway objects)

## Resources in your domain ({len(resources)})

| ID | Type | Name |
|---|---|---|
{rows}{more}

## Findings that touch your domain ({len(checks)})

| ID | Rule | Status | Severity | Observation |
|---|---|---|---|---|
{frows}

## Write

1. `{folder}/advice.json` (schema in specialists.md).
2. `{folder}/candidates.json`: a JSON array with exactly your pathway objects ({pathways}), in the scoring and output contract of
   `{SKILL_DIR}/references/scoring-and-output.md`, including the option list your pathway carries.
3. `{folder}/diagram-graph.json`, then the initial render and the final drawing:

   ```bash
   python3 "{review}/scripts/diagram.py" --graph "{folder}/diagram-graph.json" --output "{folder}" --name diagram
   ```

   Redraw with the aws-diagram-design skill (`{diagram_skill}`) as `{folder}/diagram.aws.html`, `.svg` and `.png`, keeping graph IDs as
   `data-node-id`, `data-edge-id` and `data-group-id`; run its `self_check.py` and `verify-geometry.py`, and look at the PNG.

Prices: only from the read-only helper, into your own file so parallel reviewers do not collide:

```bash
python3 "{SKILL_DIR}/scripts/list_price.py" --profile "{profile}" --id-prefix {ID_PREFIX[sid]} --output "{folder}/list-prices.json" rds|ec2|elasticache|eks ... --count N --label "..."
```

## Check before you reply

```bash
python3 "{SKILL_DIR}/scripts/advice.py" validate --run-dir "{run}" --specialist {sid}
```

Fix what it reports and run it again. Reply with its final output only.
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
        for key in ("title", "change", "benefit", "rollback"):
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
        if not rec.get("refs") or any(not ctx.known(r) for r in rec.get("refs") or []):
            errors.append(f"recommendations[{i}].refs must cite existing findings, evidence or resources")
        if not isinstance(rec.get("alternatives"), list) or not [a for a in rec.get("alternatives") or [] if nonempty(a)]:
            errors.append(f"recommendations[{i}].alternatives needs at least one (keeping and improving the current setup counts)")
        if not isinstance(rec.get("before_deciding", []), list):
            errors.append(f"recommendations[{i}].before_deciding must be a list")
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
    return []


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
    combined = fragments + [c for c in recommend.bootstrap() if c["pathway"] not in covered]
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
