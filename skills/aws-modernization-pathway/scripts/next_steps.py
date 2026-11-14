"""Next steps: which AWS services and aws-samples assets apply to the recommended pathways of one workload.

Selection is deterministic. An item appears only when its pathway is candidate or conditional AND one of its
`when` clauses matches the evidence: a FAIL/WARN/UNKNOWN finding with a listed rule_id, an in-scope resource type,
an RDS engine prefix, an EC2 platform, a keyword in the findings or candidate text, or `always_for_pathway`.
Nothing is shown for pathways that were not recommended, so a report for a workload without Oracle never offers
the Oracle migration workshop. The catalog (assets/next-steps.json) carries an official description and the AWS
page it was taken from for every service; the agent re-checks that page before quoting it in a report.
"""
import json
import re
from pathlib import Path

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "assets/next-steps.json").read_text(encoding="utf-8"))


def _text_blob(candidate, checks):
    """What the assessor wrote about this pathway: the candidate's own text plus the findings it cites. Inventory
    names are deliberately excluded (an 'eks-pod-identity-agent' add-on or a NAT 'gateway' is not agentic AI)."""
    parts = [candidate.get("problem", ""), candidate.get("rationale", ""), " ".join(candidate.get("target_services", [])),
             " ".join(candidate.get("alternatives", [])), " ".join(candidate.get("prerequisites", []))]
    parts += [c.get("observation", "") + " " + c.get("recommendation", "") for c in checks if c["id"] in candidate.get("finding_ids", [])]
    return " ".join(parts).lower()


def _matches(item, candidate, checks, inventory, scoped_types, engines, platforms, blob):
    when = item.get("when", {})
    hits = []
    if when.get("always_for_pathway"):
        hits.append("pathway recommended")
    open_rules = {c["rule_id"] for c in checks if c["status"] in {"FAIL", "WARN", "UNKNOWN"}}
    passed_rules = {c["rule_id"] for c in checks if c["status"] == "PASS"}
    if any(r in passed_rules and r not in open_rules for r in when.get("skip_when_pass", [])):
        return []  # the service is already on in this account; introducing it again reads as a catalogue
    found = sorted(set(when.get("rules", [])) & open_rules)
    if found:
        if when.get("severity"):
            severe = {c["rule_id"] for c in checks if c["rule_id"] in found and c.get("severity") == when["severity"]}
            if severe:
                hits.append("findings " + ", ".join(sorted(severe)))
        else:
            hits.append("findings " + ", ".join(found))
    types = sorted(set(when.get("resource_types", [])) & scoped_types)
    if types:
        hits.append("resources " + ", ".join(types))
    eng = [e for e in when.get("engines", []) if any(x.startswith(e) for x in engines)]
    if eng:
        hits.append("engine " + ", ".join(eng))
    plat = [p for p in when.get("platforms", []) if any(p in x for x in platforms)]
    if plat:
        hits.append("platform " + ", ".join(plat))
    words = [k for k in when.get("keywords", []) if mentioned(blob, k)]
    if words:
        hits.append("assessment mentions " + ", ".join(words))
    if when.get("all_of"):  # every listed clause kind must have matched
        kinds_hit = {h.split(" ", 1)[0] for h in hits}
        if not set(when["all_of"]) <= kinds_hit:
            return []
    # Words in the assessment text are supporting evidence, not a reason on their own: "migration" in a sentence about
    # moving a chatbot to Fargate must not surface database-migration tooling. Items whose terms name a technology the
    # inventory cannot see (mainframe, multi-agent frameworks) opt in with keywords_alone.
    if hits and all(h.startswith("assessment mentions") for h in hits) and not when.get("keywords_alone"):
        return []
    return hits


NEGATION = re.compile(r"\b(no|not|none|without|never|nor)\b[^.;:]{0,40}$")


def mentioned(blob, keyword):
    """True when the text names the keyword outside a negation: "no asynchronous flow was found" does not count."""
    start = 0
    while True:
        i = blob.find(keyword, start)
        if i < 0:
            return False
        if not NEGATION.search(blob[max(0, i - 60):i]):
            return True
        start = i + len(keyword)


def select(candidates, checks, inventory, scope_ids=None):
    """Return {pathway: {"services": [...], "assets": [...]}} for recommended pathways."""
    resources = [r for r in inventory.get("resources", []) if scope_ids is None or r["id"] in scope_ids]
    scoped_types = {r["type"] for r in resources}
    engines = {str(r["attributes"].get("Engine", "")).lower() for r in resources if r["type"].startswith("rds.")}
    platforms = {str(r["attributes"].get("PlatformDetails", "")).lower() for r in resources if r["type"] == "ec2.instance"}
    out = {}
    for candidate in candidates:
        if candidate.get("status") not in {"candidate", "conditional"}:
            continue
        pathway = candidate["pathway"]
        blob = _text_blob(candidate, checks)
        picked = {"services": [], "assets": []}
        for group in ("services", "assets"):
            for item in CATALOG[group]:
                if pathway not in item["pathways"]:
                    continue
                hits = _matches(item, candidate, checks, inventory, scoped_types, engines, platforms, blob)
                if hits:
                    entry = {k: item[k] for k in ("id", "name", "kind", "url", "why")}
                    entry["because"] = "; ".join(hits)
                    for key in ("official_description", "source", "source_checked_at", "ko"):
                        if key in item:
                            entry[key] = item[key]
                    picked[group].append(entry)
        if picked["services"] or picked["assets"]:
            out[pathway] = picked
    return out


def labels(language):
    base = CATALOG["labels"]["en"]
    return CATALOG["labels"].get((language or "en").split("-")[0], base)


def localized(item, language):
    """Catalog text in the requested language when the catalog carries it; English otherwise."""
    out = dict(item)
    ko = item.get("ko") or {}
    if (language or "en").split("-")[0] != "en":
        for key in ("why", "official_description"):
            if ko.get(key):
                out[key] = ko[key]
        words = labels(language)["because"]
        out["because"] = "; ".join(_localize_reason(part, words) for part in item.get("because", "").split("; ") if part)
    out.pop("ko", None)
    return out


def _localize_reason(part, words):
    for english, local in words.items():
        if part == english:
            return local
        if part.startswith(english + " "):
            return local + " " + part[len(english) + 1:]
    return part


def select_localized(selected, language):
    return {pathway: {group: [localized(i, language) for i in items] for group, items in groups.items()} for pathway, groups in selected.items()}


REASON_HEADS = ("pathway recommended", "assessment mentions", "findings", "resources", "engine", "platform")


def _merge_reasons(reasons):
    """'resources ec2.instance; assessment mentions container, ecs' from two pathways plus 'resources ec2.instance;
    assessment mentions kubernetes' from a third become one reason per kind with the values united, in first-seen order."""
    order, values = [], {}
    for reason in reasons:
        for part in filter(None, reason.split("; ")):
            head = next((h for h in REASON_HEADS if part == h or part.startswith(h + " ")), part)
            if head not in values:
                order.append(head); values[head] = []
            for v in filter(None, part[len(head):].strip().split(", ")) if head != part else []:
                if v not in values[head]:
                    values[head].append(v)
    return "; ".join(h + (" " + ", ".join(values[h]) if values[h] else "") for h in order)


def merged(selected, pathway_names, language="en"):
    """Each service and asset once, with every pathway it serves and every reason it matched, each reason kind once
    (HTML and Markdown share this)."""
    out = {"services": {}, "assets": {}}
    reasons = {"services": {}, "assets": {}}
    for pathway, groups in selected.items():
        for group in ("services", "assets"):
            for item in groups.get(group) or []:
                reasons[group].setdefault(item["id"], []).append(item.get("because", ""))
    for pathway, groups in select_localized(selected, language).items():
        for group in ("services", "assets"):
            for item in groups.get(group) or []:
                entry = out[group].setdefault(item["id"], dict(item, pathways=[], because_all=[]))
                entry["pathways"].append(pathway_names.get(pathway, pathway))
    words = labels(language)["because"]
    for group in ("services", "assets"):
        for key, entry in out[group].items():
            combined = _merge_reasons(reasons[group].get(key, []))
            entry["because_all"] = ["; ".join(_localize_reason(part, words) for part in combined.split("; ") if part)] if combined else []
    return out


def markdown(selected, pathway_names, language="en"):
    L = labels(language)
    if not selected:
        return [f"## {L['title']}", "", L["none"], ""]
    lines = [f"## {L['title']}", "", L["lead"], ""]
    items = merged(selected, pathway_names, language)
    if items["services"]:
        lines += [f"| {L['service']} | {L['what']} | {L['why']} | {L['source']} |", "|---|---|---|---|"]
        lines += [f"| [{s['name']}]({s['url']}) ({', '.join(s['pathways'])}) | {s['official_description']} | {s['why']} ({'; '.join(s['because_all'])}) | [{s['source'].split('/')[2]}]({s['source']}), {L['checked']} {s['source_checked_at']} |"
                  for s in items["services"].values()]
        lines.append("")
    if items["assets"]:
        lines += [f"| {L['asset']} | {L['kind']} | {L['why']} |", "|---|---|---|"]
        lines += [f"| [{a['name']}]({a['url']}) ({', '.join(a['pathways'])}) | {L['kinds'].get(a['kind'], a['kind'])} | {a['why']} ({'; '.join(a['because_all'])}) |" for a in items["assets"].values()]
        lines.append("")
    return lines
