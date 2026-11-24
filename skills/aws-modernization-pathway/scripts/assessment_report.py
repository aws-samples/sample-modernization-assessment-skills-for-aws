#!/usr/bin/env python3
"""Render the assessment report (assessment.html, with a Markdown twin) from the stage 3-5 artifacts.

One file with five pages, shown one at a time with CSS :target (no script; every page prints): 1 Executive summary (scope,
count tiles, a radar of the six pillar scores, the key improvements, where to start, a size-by-cost matrix of every
change), 2 AS-IS architecture (the AWS diagram and what the workload is made of), 3 recommendations by Well-Architected
pillar (each pillar's grade, its key issues with the suggestion and the checks that drive the grade; security is one of
the pillars), 4 Modernization recommendations (the Specialist SA Agents, each recommendation in four fields: current
state, proposal, quantitative evidence, prerequisites) and the Appendix (evidence, method and provenance, all findings,
account services, reference material, terms). Everything is English; a report in another language is translated from
this one at the end (localize_artifacts.py). The IaC change list goes to iac-changes.json only, and the order of work is left to
the meeting with the owner. Imported third-party scanner results appear as ordinary security items; the tool that
produced them is recorded in assessment-summary.json, not in the customer-facing text.

  python3 assessment_report.py --run-dir RUN_DIR --workload NAME [--key-issues key-issues.json] \\
      [--screener RUN_DIR/04-review/upstream/screener-findings.json] [--run-metadata RUN_DIR/run-metadata.json] \\
      [--as-is-diagram SVG] [--to-be-diagram SVG] [--output RUN_DIR/06-assessment]

Inputs are the canonical artifacts: 03-as-is/inventory.json + collection-manifest.json, 04-review/findings.json
+ scorecard.json, 05-modernization/pathways.json. The agent supplies key-issues.json (plain-language problem
statements that must cite real finding IDs); without it the report says so instead of inventing a summary.
Everything is rendered in English and localized with the output-language workflow. No value is estimated.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import re
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_kit as kit  # noqa: E402

PILLARS = ("Operational Excellence", "Security", "Reliability", "Performance Efficiency", "Cost Optimization", "Sustainability")
PATHWAYS = {"container": "Containers", "serverless": "Serverless", "managed-database": "Managed database", "managed-analytics": "Managed analytics",
            "genai": "Generative AI", "cloud-native": "Cloud-native structure", "modern-devops": "Modern DevOps"}
STATUS_ORDER = {"FAIL": 0, "WARN": 1, "UNKNOWN": 2, "PASS": 3, "NOT_APPLICABLE": 4}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
CONFIDENCE_LABEL = {"configuration": "API configuration", "template_declared": "Template declaration", "source_declared": "Source declaration",
                    "runtime_observed": "Runtime observation", "measured": "Measured", "documented": "Documented",
                    "inferred": "Inferred", "unverified": "Unverified"}
REFERENCE_HOSTS = {"docs.aws.amazon.com", "aws.amazon.com", "repost.aws"}
REFERENCES = json.loads((Path(__file__).resolve().parents[1] / "assets/references.json").read_text(encoding="utf-8"))


def target_of(c):
    """What the finding is about, in words a reader can find in the console."""
    value = str(c.get("target") or "") or (c["resource_id"].split(":", 3)[-1] if c.get("resource_id") else "") or c.get("upstream_resource") or "-"
    if value.startswith("arn:"):
        value = value.split(":", 5)[-1]  # the resource part: loadbalancer/app/name/id, repository/name, secret:name
    return value


ACRONYMS = {"IAM", "S3", "EKS", "ECS", "ECR", "RDS", "EC2", "EBS", "EIP", "ELB", "ALB", "NLB", "TLS", "SSL", "IMDSV2", "IMDS", "KMS", "SNS", "SQS", "VPC", "ACM", "ASG", "CI", "CD", "IAC",
            "OS", "DB", "API", "WAF", "PITR", "BPA", "AZ", "CIDR", "SG", "DNS", "HTTP", "HTTPS", "AWS", "GENAI", "K8S", "SSM", "CPU", "RTO", "RPO", "MFA", "ARM64", "ARM", "X86", "OIDC",
            "EFS", "DMS", "MSK", "AI", "ML", "LLM", "RAG", "URL", "IP", "ID", "TA", "CW", "ENI", "NAT", "TGW", "VPN", "DX", "WA", "PDB", "HPA", "CF", "CFN", "CDK", "SSH", "RDP"}
FIXED = {"IMDSV2": "IMDSv2", "IAC": "IaC", "GENAI": "GenAI", "K8S": "Kubernetes", "X86": "x86", "ARM64": "arm64"}


def rule_words(rule):
    """A rule code in words: ALARM_NOTIFICATION_TARGET -> 'Alarm notification target', EKS_ADMIN_IAM_USER -> 'EKS admin IAM user'."""
    out = []
    for i, w in enumerate(rule.split("_")):
        if w.upper() in FIXED:
            out.append(FIXED[w.upper()])
        elif w.upper() in ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w.capitalize() if i == 0 else w.lower())
    return " ".join(out)


def rule_label(c):
    rule = c["rule_id"]
    return rule.split("::", 2)[-1] if rule.startswith("SS::") else rule_words(rule)


def confidence_chip(c):
    return kit.chip(CONFIDENCE_LABEL.get(c.get("confidence", "unverified"), c.get("confidence", "unverified")), kit.MUTED)


def valid_reference(item):
    url = item.get("url", "") if isinstance(item, dict) else ""
    host = urlparse(url).hostname or ""
    return (url.startswith("https://") and isinstance(item.get("title"), str) and item["title"].strip()
            and (host in REFERENCE_HOSTS or host.endswith(".docs.aws.amazon.com")))


def issue_references(issue, by_id):
    """Agent-cited references first (validated), then the curated rule map, then the findings' own sources."""
    out, seen = [], set()

    def add(title, url):
        if url not in seen and len(out) < 5:
            seen.add(url); out.append({"title": title, "url": url})
    for item in issue.get("references", []) or []:
        if not valid_reference(item):
            raise ValueError(f"key issue reference must be an https AWS documentation link with a title: {item!r}")
        add(item["title"], item["url"])
    for fid in issue["finding_ids"]:
        for key in REFERENCES["rules"].get(by_id[fid]["rule_id"], []):
            ref = REFERENCES["references"][key]
            add(ref["title"], ref["url"])
    catalog = {ref["url"].split("#")[0]: ref["title"] for ref in REFERENCES["references"].values()}
    for fid in issue["finding_ids"]:
        for url in by_id[fid].get("source_urls", []):
            if url.split("#")[0] in catalog:  # only pages whose title was verified
                add(catalog[url.split("#")[0]], url)
    return out


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_svg(path):
    """The diagram text as rendered, after report_kit.scan_svg refused DTDs, scripts, handlers and external references."""
    text = Path(path).read_text(encoding="utf-8")
    kit.scan_svg(text)
    return text


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def load_list_prices(run):
    """05-modernization/list-prices.json written by list_price.py: published On-Demand prices with their basis."""
    path = run / "05-modernization/list-prices.json"
    if not path.is_file():
        return {}
    return {p["id"]: p for p in read_json(path).get("prices", [])}


def load_key_issues(path, finding_ids, prices=None):
    """Agent-authored plain-language issues; each must cite existing findings and carry a suggestion, not an order.

    `action` is the suggestion the reader sees under "Suggestion": at least two sentences that say what would improve and
    how, in the tone of an architect advising a customer whose constraints are not all known (the configuration may be
    deliberate). A one-line imperative is refused so the report never reads as a list of commands."""
    if path is None:
        return []
    data = read_json(path)
    issues = data["issues"] if isinstance(data, dict) else data
    if not isinstance(issues, list) or not issues:
        raise ValueError("key-issues must be a non-empty list")
    clean = []
    for i, item in enumerate(issues, 1):
        for field in ("title", "problem", "impact", "action"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"key issue {i} needs {field}")
        refs = item.get("finding_ids") or []
        if not refs or any(r not in finding_ids for r in refs):
            raise ValueError(f"key issue {i} must cite existing finding IDs")
        language = kit.english_errors([(f"key issue {i} {f}", item.get(f, "")) for f in ("title", "problem", "impact", "action")]
                                      + [(f"key issue {i} first_step", (item.get("first_step") or {}).get("text", ""))])
        if language:
            raise ValueError(language[0])
        if item.get("urgency") not in {"now", "first-wave", "later"}:
            raise ValueError(f"key issue {i} urgency must be now, first-wave or later")
        if len(sentences(item["action"])) < 2:
            raise ValueError(f"key issue {i}: action must be a suggestion of at least two sentences (what would improve, how, and the alternative when the "
                             "customer's constraints differ), not a one-line instruction")
        for field, allowed in (("effort", EFFORT), ("cost_direction", COST_DIRECTION), ("owner", OWNER)):
            if item.get(field) is not None and item[field] not in allowed:
                raise ValueError(f"key issue {i}: {field} must be one of {', '.join(allowed)}")
        first = item.get("first_step")
        if first is not None and (not isinstance(first, dict) or not str(first.get("text", "")).strip() or first.get("effort") not in {"setting", "days"}):
            raise ValueError(f"key issue {i}: first_step needs text and effort setting or days (the small, cost-neutral part that can start this week)")
        priced = []
        for ref in item.get("list_prices") or []:  # [{"id": "p001", "sign": "+"}]: only prices list_price.py actually returned
            if not isinstance(ref, dict) or ref.get("sign") not in ("+", "-") or ref.get("id") not in (prices or {}):
                raise ValueError(f"key issue {i}: list_prices entries need an id from 05-modernization/list-prices.json and sign + or -")
            priced.append(dict((prices or {})[ref["id"]], sign=ref["sign"]))
        clean.append({"title": item["title"], "problem": item["problem"], "impact": item["impact"], "action": item["action"],
                      "finding_ids": refs, "urgency": item["urgency"], "pillar": item.get("pillar"),
                      "effort": item.get("effort"), "cost_direction": item.get("cost_direction"), "owner": item.get("owner"), "list_prices": priced,
                      "first_step": first,
                      "references": item.get("references", [])})
    return clean


# Size, cost direction and who can do it: fixed vocabularies so the reader can sort the issues without the report
# inventing durations or amounts. Labels are English drafts; the glossary carries the Korean.
EFFORT = {"setting": ("Setting change", "a few configuration values or one template line; hours"), "days": ("Days of work", "a small change with a test and a deployment"),
          "weeks": ("Weeks of work", "several components or a new pipeline; needs a plan"), "quarter": ("A quarter or more", "architecture change; needs a decision first")}
COST_DIRECTION = {"up": ("Cost up", "#EC7211"), "neutral": ("Cost neutral", "#7D8998"), "down": ("Cost down", "#7AA116"), "measure_first": ("Measure first", "#0972D3")}
OWNER = {"platform": "Platform/DevOps setting", "application": "Application change needed", "account": "Account administrator", "provider": "Operations provider (SI/MSP)"}


def net_list_price(issue):
    prices = issue.get("list_prices") or []
    return round(sum(p["monthly_usd"] * (1 if p["sign"] == "+" else -1) for p in prices), 2) if prices else None


def usd(value):
    return f"{'+' if value > 0 else '−' if value < 0 else ''}${abs(value):,.0f}"


def is_example(issue):
    """A resize whose target size waits for measurement: the saving is one possible configuration, not a plan. A price that
    only adds cost (an extended-support surcharge if nothing changes) is a published fact and stays a list price."""
    return issue.get("cost_direction") == "measure_first" and any(p["sign"] == "-" for p in issue.get("list_prices") or [])


def price_lines(issue):
    prices = issue.get("list_prices") or []
    if not prices:
        return ""
    # The amount is its own element so a label written in the output language does not share a unit with "/month".
    rows = "".join(f'<li><span>{kit.esc(p["sign"])} ${p["monthly_usd"]:,.0f}/month</span> · {kit.esc(p["label"])} <span class="small">({kit.esc(p["basis"])}; {kit.esc(p["id"])})</span></li>' for p in prices)
    example = is_example(issue)
    lead = ("(an example at published On-Demand rates for one possible configuration; the size is decided after measurement, so the figure is not a saving or a cost to plan on)"
            if example else "(AWS Price List API, On-Demand; not a bill forecast)")
    return (f'<div class="small"><b>List prices</b> net {kit.esc(usd(net_list_price(issue)))}/month {kit.esc(lead)}<ul class="plain">{rows}</ul></div>')


def price_table(prices):
    """Markdown list prices as a table: the amount in a numeric cell, the label (often written in the output language) in its
    own cell, so localization never sees "/month" glued to a sentence it preserves."""
    return (["| Sign | USD per month | What | Basis | ID |", "|---|---:|---|---|---|"]
            + [f"| {p['sign']} | {p['monthly_usd']:,.0f} | {str(p['label']).replace('|', '/')} | {str(p['basis']).replace('|', '/')} | {p['id']} |" for p in prices] + [""])


def issue_chips(issue):
    chips = ""
    net = net_list_price(issue)
    if net is not None:
        example = is_example(issue)
        chips += kit.chip(f"{'Example, before measurement: ' if example else 'List price '}{usd(net)}/mo", kit.MUTED if example else ("#7AA116" if net < 0 else "#EC7211" if net > 0 else kit.MUTED))
    if issue.get("effort") in EFFORT:
        chips += kit.chip(EFFORT[issue["effort"]][0], kit.INK)
    if issue.get("cost_direction") in COST_DIRECTION:
        label, color = COST_DIRECTION[issue["cost_direction"]]
        chips += kit.chip(label, color)
    if issue.get("owner") in OWNER:
        chips += kit.chip(OWNER[issue["owner"]], kit.MUTED)
    return chips


DATED_RULES = {"EKS_VERSION_SUPPORT": "Kubernetes standard support ends", "RDS_ENGINE_SUPPORT": "Database engine standard support ends",
               "LAMBDA_RUNTIME_DEPRECATION": "Lambda runtime deprecation", "ACM_CERTIFICATE_EXPIRY": "Certificate expires", "EC2_OS_END_OF_LIFE": "Operating system end of life"}


def dated_items(checks):
    """(date, label, target, finding id) for findings that carry a calendar date the business cannot move."""
    out = []
    for c in checks or []:
        if c["rule_id"] in DATED_RULES and c["status"] in {"FAIL", "WARN"}:
            found = re.findall(r"\b(20\d\d-\d\d-\d\d)\b", c.get("observation", ""))
            if found:
                out.append((max(found) if c["status"] == "WARN" else min(found), DATED_RULES[c["rule_id"]], target_of(c), c["id"]))
    return sorted(set(out))


def board_groups(issues):
    """(quick, invest, firsts) as (number, issue) pairs: quick = small and not raising cost; invest = the rest of the sized
    issues; firsts = the cost-neutral first steps of issues that are not quick themselves."""
    sized = [i for i in issues if i.get("effort") in EFFORT]
    numbered = list(enumerate(issues, 1))
    quick = [(n, i) for n, i in numbered if i in sized and i["effort"] in {"setting", "days"} and i.get("cost_direction") not in {"up", "measure_first"}]
    invest = [(n, i) for n, i in numbered if i in sized and (n, i) not in quick]
    firsts = [(n, i) for n, i in numbered if i.get("first_step") and (n, i) not in quick]
    return quick, invest, firsts


def quick_wins_html(issues, checks=None):
    """The decision page in three lists: what can start this week (small and cost-neutral), what needs an investment
    decision (larger work, a cost increase or a size still to measure, with the net list price), and what has a date the
    business cannot move. Built only from what the issues declare and the dated findings."""
    sized = [i for i in issues if i.get("effort") in EFFORT]
    dated = dated_items(checks)
    if not sized and not dated:
        return ""
    quick, invest, first_issues = board_groups(issues)

    def items(group, empty="none declared"):
        return "".join(f'<li><a href="#issue-{n}">{kit.esc(i["title"])}</a> {issue_chips(i)}</li>' for n, i in group) or f'<li class="small">{kit.esc(empty)}</li>'
    # First steps of larger issues: the cost-neutral part (subscribe the topic, set DeletionProtection) that need not wait.
    firsts = "".join(f'<li>{kit.esc(i["first_step"]["text"])} {kit.chip(EFFORT[i["first_step"]["effort"]][0], kit.INK)} <a href="#issue-{n}" class="small">issue {n}</a></li>'
                     for n, i in first_issues)
    # No total across issues: one issue's price is a cost of acting (a reader), another's a cost of not acting (an
    # extended-support surcharge); adding them gives a number nobody would pay.
    total = " Each item shows its own published list price where one was looked up."
    dated_rows = "".join(f'<li><b>{kit.esc(d)}</b> · {kit.esc(label)} · <code>{kit.esc(target)}</code> <a href="#{kit.esc(fid)}" class="small">{kit.esc(fid)}</a></li>'
                         for d, label, target, fid in dated) or '<li class="small">no dated deadline found</li>'
    return (f'<div class="grid g3 quickwins"><div class="card"><h3>Can start this week</h3><p class="small">Setting changes and small pieces of work that do not raise cost.</p>'
            f'<ul class="plain">{items(quick, "") if quick else ""}{firsts}{"" if (quick or firsts) else "<li class=small>none declared</li>"}</ul></div>'
            f'<div class="card"><h3>Needs an investment decision</h3><p class="small">Weeks of work, an architecture change, a cost increase or a size still to be measured.{kit.esc(total)}</p>'
            f'<ul class="plain">{items(invest)}</ul></div>'
            f'<div class="card"><h3>Has a date</h3><p class="small">Deadlines set by AWS support calendars or certificates; they do not move with the plan.</p>'
            f'<ul class="plain">{dated_rows}</ul></div></div>'
            f'<p class="small">Size words: ' + "; ".join(f"<b>{kit.esc(v[0])}</b> = {kit.esc(v[1])}" for v in EFFORT.values()) + ". They describe the change, not a team's calendar.</p>")


# Only identifiers that name an IAM user: the IAM ARN form and the EKS access-entry form. A plain "/user/" path segment
# (documentation URLs such as .../inspector/latest/user/getting_started...) is not a person.
USER_ARN = re.compile(r"((?:arn:aws:iam::\d{12}:user/(?:[\w+=,.@-]+/)*)|(?:access-entry/[\w.-]+/user/\d{12}/))([A-Za-z][A-Za-z0-9+=,.@_-]{1,63})")
# "IAM user name" in the English draft and "IAM <user, in Korean> name" in text the agents wrote in the output language.
USER_WORD = re.compile(r"((?:IAM user|IAM \uc0ac\uc6a9\uc790)\s?)([A-Za-z][A-Za-z0-9+=,.@_-]{1,63})")
# Names of the IAM users the evidence contains (from their ARNs), masked wherever they appear in prose. Generic names that
# are also ordinary words stay, so a user called "admin" does not blank out "admin port".
KNOWN_USERS = set()
GENERIC_NAMES = {"admin", "administrator", "root", "user", "users", "test", "dev", "ops", "deploy", "deployer", "ci", "cd", "bot", "service", "readonly", "operator"}


def iam_user_names(*documents):
    """IAM user names found in user ARNs and EKS access-entry ARNs anywhere in the given JSON documents."""
    text = json.dumps(documents, ensure_ascii=False)
    return {m.group(2) for m in USER_ARN.finditer(text) if len(m.group(2)) >= 3 and m.group(2).lower() not in GENERIC_NAMES}


PLAIN = {"(template_declared)": "(from the template)", "(source_declared)": "(from source code)", "(runtime_observed)": "(seen on the host)",
         "(inferred)": "(inferred)", "template_declared": "template declaration", "source_declared": "source declaration", "runtime_observed": "seen on the host",
         "scope.json": "the scope interview",
         # Evidence levels written into an observation next to an evidence id ("(configuration, s0032)") read as a label to
         # localize, not a code: "API configuration" is a glossary term the localizer must translate.
         "(configuration, ": "(API configuration, ", "(configuration)": "(API configuration)"}
PLAIN_RE = re.compile("|".join(re.escape(k) for k in sorted(PLAIN, key=len, reverse=True)))


def plain_words(text):
    """Evidence-level codes and file names that agents write into prose become words; the codes stay in the JSON."""
    return PLAIN_RE.sub(lambda m: PLAIN[m.group(0)], text)


# Words that follow "IAM user" in ordinary prose ("one IAM user with long-term keys") and are not names.
PROSE_AFTER_USER = {"with", "without", "has", "had", "have", "that", "which", "who", "whose", "and", "or", "in", "on", "for",
                    "from", "to", "of", "is", "was", "are", "can", "could", "would", "may", "might", "keys", "key",
                    "credentials", "credential", "access", "accounts", "account", "names", "name", "policies", "policy",
                    "permissions", "uses", "used", "holds", "owns", "still", "also", "as", "at", "by", "via"}


def mask_people(text):
    """Individual IAM user names become an initial plus asterisks in the customer-facing report: the finding is about a
    long-lived credential, not about a person, and the full identifier stays in findings.json and the evidence files."""
    def initial(m):
        return m.group(1) + m.group(2)[0] + "***"

    def initial_unless_prose(m):
        return m.group(0) if m.group(2).lower() in PROSE_AFTER_USER else initial(m)
    text = USER_WORD.sub(initial_unless_prose, USER_ARN.sub(initial, text))
    for name in sorted(KNOWN_USERS, key=len, reverse=True):
        # "user/<name>" on its own (an EKS access entry's principal, a shortened ARN) is the same person.
        text = re.sub(rf"(\buser/){re.escape(name)}(?![A-Za-z0-9_@+=-])", lambda m: m.group(1) + name[0] + "***", text)
        text = re.sub(rf"(?<![A-Za-z0-9_@.+=/-]){re.escape(name)}(?![A-Za-z0-9_@+=-])", name[0] + "***", text)
    return text


def anchor(pillar):
    return "pillar-" + pillar.lower().replace(" ", "-")


def sources_html(c):
    return " · ".join(f'<a href="{kit.esc(u)}" target="_blank" rel="noopener">{kit.esc(u.split("/")[2] if "//" in u else u)}</a>' for u in c.get("source_urls", []))


def related_chips(c):
    return "".join(kit.chip("also " + p, kit.INK) for p in c.get("related_pillars", []) or [])


def finding_card(c):
    label, color, _ = kit.STATUS[c["status"]]
    sev_label, sev_color = kit.SEVERITY.get(c.get("severity"), ("Info", kit.SOFT))
    return (f'<article class="finding" id="{kit.esc(c["id"])}" style="border-left-color:{color}"><header><strong>{kit.esc(c["id"])} · {kit.esc(rule_label(c))}</strong> <code class="small">{kit.esc(c["rule_id"])}</code>'
            f'{kit.status_chip(c["status"])}{kit.chip("severity " + sev_label, sev_color)}{confidence_chip(c)}{related_chips(c)}</header>'
            f'<p class="small">Target <code>{kit.esc(target_of(c))}</code></p><p><b>What we saw</b> {kit.esc(c["observation"])}</p>'
            f'<p><b>What to do</b> {kit.esc(c["recommendation"])}</p>'
            f'<div class="ev">Evidence {kit.evidence_links(c.get("evidence_ids"))} · Sources {sources_html(c) or "-"}</div></article>')


def group_findings(items):
    """One group per (rule, status, severity, recommendation): the same advice for several resources is one item."""
    groups, order = {}, []
    for c in items:
        key = (c["rule_id"], c["status"], c.get("severity"), c["recommendation"])
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append(c)
    return [groups[k] for k in order]


def finding_cards_grouped(items):
    for members in group_findings(items):
        if len(members) == 1:
            yield finding_card(members[0])
            continue
        first = members[0]
        label, color, _ = kit.STATUS[first["status"]]
        sev_label, sev_color = kit.SEVERITY.get(first.get("severity"), ("Info", kit.SOFT))
        same = len({m["observation"] for m in members}) == 1
        rows = "".join(f"<li><code>{kit.esc(target_of(m))}</code>{'' if same else ' ' + kit.esc(m['observation'])} <span class=\"small\">{kit.esc(m['id'])}</span></li>" for m in members)
        evidence = sorted({e for m in members for e in m.get("evidence_ids", [])})
        seen_text = kit.esc(first["observation"]) if same else f"{len(members)} resources, details below."
        yield (f'<article class="finding" id="{kit.esc(first["id"])}" style="border-left-color:{color}"><header><strong>{kit.esc(rule_label(first))} · {len(members)} resources</strong>'
               f'{kit.status_chip(first["status"])}{kit.chip("severity " + sev_label, sev_color)}{confidence_chip(first)}{related_chips(first)}</header>'
               f'<p><b>What we saw</b> {seen_text}</p><ul class="small">{rows}</ul><p><b>What to do</b> {kit.esc(first["recommendation"])}</p>'
               + "".join(f'<span id="{kit.esc(m["id"])}"></span>' for m in members[1:]) +
               f'<div class="ev">Findings {kit.esc(", ".join(m["id"] for m in members))} · Evidence {kit.evidence_links(evidence)} · Sources {sources_html(first) or "-"}</div></article>')


# Priority labels invite a conversation; nothing here is an order. "Discuss first" replaces the former "Act now".
URGENCY = {"now": ("1 · Decide first", "#EC7211"), "first-wave": ("2 · Plan next", "#0972D3"), "later": ("3 · Later", "#7D8998")}
URGENCY_LABEL = {k: v[0] for k, v in URGENCY.items()}


# Signals that describe something going on right now (a log stream that stopped, a version whose support ends on a
# known date, a certificate about to expire, a flagged Trusted Advisor check): the reader checks these before planning.
LIVE_SIGNAL_RULES = {"LOG_DELIVERY": "Log delivery", "OPS_LOG_DELIVERY": "Log delivery", "EKS_VERSION_SUPPORT": "Kubernetes version support", "RDS_ENGINE_SUPPORT": "Database engine support",
                     "LAMBDA_RUNTIME_DEPRECATION": "Lambda runtime", "ACM_CERTIFICATE_EXPIRY": "Certificate expiry", "EC2_OS_END_OF_LIFE": "Operating system support",
                     "HEALTH_OPEN_EVENTS": "AWS Health event", "TRUSTED_ADVISOR_FLAGGED": "Trusted Advisor", "CI_RECENT_FAILURES": "Pipeline failures", "STACK_DRIFT_DETECTED": "Stack drift"}


def live_signals_html(checks):
    items = [c for c in checks if c["rule_id"] in LIVE_SIGNAL_RULES and c["status"] in {"FAIL", "WARN"}]
    if not items:
        return ""
    items.sort(key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
    rows = "".join(f'<li><b>{kit.esc(LIVE_SIGNAL_RULES[c["rule_id"]])}</b> · <code>{kit.esc(target_of(c))}</code> — {kit.esc(c["observation"])} <a href="#{kit.esc(c["id"])}">{kit.esc(c["id"])}</a></li>'
                   for c in items[:8])
    more = f'<li class="small">+{len(items) - 8} more in the findings</li>' if len(items) > 8 else ""
    return (f'<div class="callout"><p><b>Happening now.</b> These observations describe the current state rather than a design choice; worth a look before the planning items below.</p>'
            f'<ul class="plain">{rows}{more}</ul></div>')


def issues_html(issues):
    if not issues:
        return ('<div class="callout"><p><b>Key issues were not authored for this run.</b> The findings and grades below are complete, but the plain-language '
                'summary requires the assessing agent to supply key-issues.json (problem, impact, action and the finding IDs behind each).</p></div>')
    urgency = URGENCY
    out = []
    for n, issue in enumerate(issues, 1):
        label, color = urgency[issue["urgency"]]
        refs = " ".join(f'<a href="#{kit.esc(r)}">{kit.esc(r)}</a>' for r in issue["finding_ids"])
        links = "".join(f'<li><a href="{kit.esc(r["url"])}" target="_blank" rel="noopener">{kit.esc(r["title"])}</a></li>' for r in issue.get("resolved_references", []))
        out.append(f'<article class="issue" id="issue-{n}" style="border-left-color:{color}"><h3>{n}. {kit.esc(issue["title"])} <span class="chips">{kit.chip(label, color)}{issue_chips(issue)}</span></h3>'
                   f'<p>{kit.esc(issue["problem"])}</p><p class="why"><b>Why it matters</b> {kit.esc(issue["impact"])}</p>'
                   f'<p class="act"><b>Suggestion</b> {kit.esc(issue["action"])}</p>{price_lines(issue)}'
                   + (f'<div class="small"><b>References</b><ul>{links}</ul></div>' if links else "") +
                   f'<div class="ref">Findings {refs}</div></article>')
    return "".join(out)


def strengths_html(checks):
    """What is already in good shape, from the PASS checks: the rules with the most passing resources and the account
    services that are on. Readers who only see red tiles reach for the defensive; this keeps the picture balanced."""
    passed = [c for c in checks if c["status"] == "PASS"]
    if not passed:
        return ""
    by_rule = Counter(c["rule_id"] for c in passed)
    sample = {}
    for c in passed:
        sample.setdefault(c["rule_id"], c)
    top = [(r, n) for r, n in by_rule.most_common() if r not in ACCOUNT_RULES][:6]
    items = "".join(f'<li><b>{kit.esc(rule_label(sample[r]))}</b> <span class="small">· {n} resource(s) · {kit.esc(sample[r]["observation"][:120])}</span></li>' for r, n in top)
    enabled = [c for c in passed if c["rule_id"] in ACCOUNT_RULES and c.get("enablement") == "enabled"]
    if enabled:
        items += f'<li><b>Account services on</b> <span class="small">· {kit.esc(", ".join(ACCOUNT_RULE_LABEL.get(c["rule_id"], c["rule_id"]) for c in enabled))}</span></li>'
    return (f'<div class="card" style="margin-top:14px"><h3>Already in good shape</h3><p class="small">{len(passed)} check(s) passed across {len(by_rule)} rule(s). '
            f'The grades below weigh failures; this is the other side of the ledger.</p><ul class="plain">{items}</ul></div>')


def pillar_sections(checks, grades, screener, context=None):
    """Findings per pillar. Open questions (UNKNOWN checks the configuration cannot answer) are listed in the pillar of
    their first check, one line per distinct question; questions the scope interview already answered keep only their
    anchors."""
    groups = question_groups(checks, context)
    home = defaultdict(list)
    for text, members in groups:
        home[members[0]["pillar"]].append((text, members))
    asked = {m["id"] for _, members in groups for m in members}
    out = []
    for pillar in PILLARS:
        key, info = grades[pillar]
        items = sorted([c for c in checks + screener if c["pillar"] == pillar and c["status"] in {"FAIL", "WARN"}],
                       key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
        passed = [c for c in checks if c["pillar"] == pillar and c["status"] in {"PASS", "NOT_APPLICABLE"}]
        questions = [c for c in checks if c["pillar"] == pillar and c["status"] == "UNKNOWN"]
        body = "".join(finding_cards_grouped(items))
        if not body:
            body = (f'<p>Nothing failed or warned in this pillar: {len(passed)} check(s) passed.'
                    + (f' The grade stays at Watch because {len(questions)} question(s) cannot be answered from configuration; the answers decide whether anything here needs work.' if questions else "")
                    + '</p>')
        mine = home.get(pillar, [])
        if mine:
            listed = "".join(f'<li id="q-{kit.esc(members[0]["id"])}">' + "".join(f'<span id="{kit.esc(m["id"])}"></span>' for m in members)
                             + "".join(f'<span id="q-{kit.esc(m["id"])}"></span>' for m in members[1:])
                             + f'{kit.esc(text)} <span class="small"><code>{kit.esc(", ".join(dict.fromkeys(target_of(m) for m in members))[:160])}</code></span></li>'
                             for text, members in mine)
            body += (f'<p class="small"><b>Open questions for this pillar</b> ({len(mine)}): configuration cannot answer these, and the answers decide severity and priority.</p>'
                     f'<ul class="plain">{listed}</ul>')
        body += "".join(f'<span id="{kit.esc(q["id"])}"></span><span id="q-{kit.esc(q["id"])}"></span>' for q in questions if q["id"] not in asked)
        if passed:
            grouped = Counter((c["rule_id"], c["status"]) for c in passed)
            first = {(c["rule_id"], c["status"]): c["observation"] for c in reversed(passed)}
            rows = "".join(f'<tr><td><code>{kit.esc(r)}</code></td><td>{kit.status_chip(s)}</td><td>{n}</td><td>{kit.esc(first[(r, s)])}</td></tr>' for (r, s), n in sorted(grouped.items()))
            body += "".join(f'<span id="{kit.esc(c["id"])}"></span>' for c in passed)
            body += (f'<details><summary class="small">Checks that passed or do not apply ({len(passed)})</summary><div class="body">'
                     f'<table><thead><tr><th>Rule</th><th>Status</th><th>Count</th><th>Observation</th></tr></thead><tbody>{rows}</tbody></table></div></details>')
        out.append(f'<details id="{anchor(pillar)}"{" open" if key in {"critical", "weak"} else ""}><summary>{kit.esc(pillar)} {kit.grade_chip(key)}'
                   f'<span class="small">{kit.esc(kit.grade_counts_text(info))}</span></summary><div class="body"><p class="small">{kit.esc(kit.GRADES[key][3])}</p>{body}</div></details>')
    return "".join(out)


STRENGTH = {"HIGH": ("Strongly recommended", "#DD344C"), "MEDIUM": ("Recommended", "#EC7211"), "LOW": ("Optional", "#7D8998"), "INFO": ("Optional", "#7D8998")}


def security_chapter(checks, screener):
    items = sorted([c for c in checks + screener if c["pillar"] == "Security" and c["status"] in {"FAIL", "WARN"}],
                   key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
    counts = Counter(c["severity"] for c in items)
    def rows_for(severity=None):
        return [(f"#{c['id']}", f"{c['id']} · {rule_label(c)} · {target_of(c)}") for c in items if severity is None or c.get("severity") == severity]
    tiles = (kit.kpi(len(items), "security findings", "fail" if counts.get("HIGH") else "warn", rows_for()) + kit.kpi(counts.get("HIGH", 0), "high severity", "fail", rows_for("HIGH"))
             + kit.kpi(counts.get("MEDIUM", 0), "medium severity", "warn", rows_for("MEDIUM")) + kit.kpi(counts.get("LOW", 0), "low severity", "unknown", rows_for("LOW")))
    rows = []
    for members in group_findings(items):
        first = members[0]
        targets = [target_of(m) for m in members]
        res = "".join(f"<li><code>{kit.esc(t)}</code></li>" for t in targets[:3]) + (f'<li class="small">+{len(targets) - 3} more</li>' if len(targets) > 3 else "")
        sev = first.get("severity", "INFO")
        strength, strength_color = STRENGTH.get(sev, STRENGTH["LOW"])
        rows.append(f'<tr><td><a href="#{kit.esc(first["id"])}">{kit.esc(rule_label(first))}</a></td><td>{kit.chip(kit.SEVERITY[sev][0], kit.SEVERITY[sev][1])}</td>'
                    f'<td>{kit.chip(strength, strength_color)}</td><td><ul class="plain">{res}</ul></td><td>{kit.esc(first["recommendation"])}</td></tr>')
    table = ('<table><thead><tr><th>Check</th><th>Severity</th><th>How firmly</th><th>Resource</th><th>Recommendation</th></tr></thead><tbody>'
             + ("".join(rows) or '<tr><td colspan="5" class="small">No security failures or warnings.</td></tr>') + "</tbody></table>")
    in_place = Counter(c["rule_id"] for c in checks if c["pillar"] == "Security" and c["status"] == "PASS")
    needs = Counter(c["rule_id"] for c in items)
    sample = {c["rule_id"]: c for c in checks + screener if c["pillar"] == "Security"}
    ledger = (f'<div class="grid g2" style="margin:12px 0"><div class="card"><h3>In place today</h3><p class="small">Security checks that passed; what can be shown now.</p><ul class="plain">'
              + ("".join(f'<li>{kit.esc(rule_label(sample[r]))} <span class="small">· {n} resource(s)</span></li>' for r, n in in_place.most_common(12)) or '<li class="small">none</li>')
              + f'</ul></div><div class="card"><h3>Needs a change</h3><p class="small">Security checks that failed or warned; what can be shown after the change.</p><ul class="plain">'
              + ("".join(f'<li>{kit.esc(rule_label(sample[r]))} <span class="small">· {n} resource(s)</span></li>' for r, n in needs.most_common(12)) or '<li class="small">none</li>')
              + '</ul></div></div>')
    return (f'<div class="kpis">{tiles}</div>{ledger}<p class="small">"How firmly" reads the severity for you: <b>Strongly recommended</b> items are the ones to decide on first; '
            f'<b>Recommended</b> items are worth planning; <b>Optional</b> items depend on the workload\'s requirements. The wording of each recommendation stays a suggestion '
            f'because the final call is the workload owner\'s.</p>{table}')


def iac_changes(checks):
    rows = []
    for c in checks:
        for ch in c.get("iac_changes", []) or []:
            rows.append(dict(ch, finding_id=c["id"], rule_id=c["rule_id"], severity=c.get("severity"), status=c["status"]))
    rows.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["stack"], r["logical_id"]))
    return rows


SUPPORT_PLAN_RULES = {"TRUSTED_ADVISOR_ACCESS", "HEALTH_OPEN_EVENTS"}


def answered_by_scope(c, context):
    """True when the scope interview already holds the answer, so the report must not ask the owner again."""
    plan = (context or {}).get("support_plan", "unknown")
    return c["rule_id"] in SUPPORT_PLAN_RULES and plan not in (None, "", "unknown")


def scope_context_text(context):
    """What the scope interview already settled (environment, operator, support plan, obligations), in one sentence."""
    parts = []
    env = (context or {}).get("environment", "unknown"); op = (context or {}).get("operator", "unknown"); plan = (context or {}).get("support_plan", "unknown")
    comp = [x for x in (context or {}).get("compliance", []) if x != "unknown"]
    if env != "unknown":
        parts.append(f"environment: {env}")
    if op != "unknown":
        parts.append(f"operated by: {op}")
    if plan != "unknown":
        parts.append(f"support plan: {plan}")
    if comp:
        parts.append(f"compliance: {', '.join(comp)}")
    return f"Known from the scope interview and not asked again: {'; '.join(parts)}." if parts else ""


def compliance_callout(context, regions=None):
    """When the scope interview could not say which obligations apply ("unknown"), say what would change with them:
    data-protection and industry rules and where the data must stay decide whether several account services and the
    region choice matter. Nothing when the scope names obligations, says none, or predates the question."""
    comp = (context or {}).get("compliance")
    if not isinstance(comp, list) or comp != ["unknown"]:
        return ""
    where = ", ".join(regions or [])
    return ('<div class="callout"><p><b>Compliance obligations.</b> The scope interview did not name any. If the workload handles personal or health data, or falls '
            'under rules such as ISMS-P or PCI DSS, the services below that keep continuous evidence (Security Hub standards, the Config recorder, Inspector, '
            'IAM Access Analyzer) move from optional to worth enabling'
            + (f', and whether the data may stay in {kit.esc(where)} is worth confirming' if where else '') + '.</p></div>')


def question_groups(checks, context=None):
    """[(question text, [checks])] for open questions not answered by the scope interview: one group per rule when a rule
    asks about several resources, otherwise one per distinct sentence. Shared by the pillar sections and the summary
    tile so both always show the same count."""
    questions = [c for c in checks if c["status"] == "UNKNOWN" and not answered_by_scope(c, context)]
    per_rule = Counter(c["rule_id"] for c in questions)
    grouped, order = {}, []
    for c in questions:
        key = ("rule", c["rule_id"]) if per_rule[c["rule_id"]] > 1 else ("text", re.sub(r"\s+", " ", (c.get("question") or c["recommendation"]).strip()))
        if key not in grouped:
            grouped[key] = []; order.append(key)
        grouped[key].append(c)
    out = []
    for key in order:
        members = grouped[key]; first = members[0]
        text = first.get("question") or first["recommendation"]
        if len(members) > 1:
            for name in filter(None, (target_of(first), first.get("target"))):
                text = text.replace(str(name), "the resources listed")
        out.append((text, members))
    return out


def pathway_cards(pathways):
    """Cards for the directions worth acting on; directions that are not recommended or not assessed yet collapse into one
    short table (what it would take, when the evidence allows saying), so the reader does not page through 'nothing to do'."""
    out, rest = [], []
    for c in pathways:
        if c["status"] not in {"candidate", "conditional"}:
            rest.append(c)
            continue
        services = "".join(f"<li>{kit.esc(s)}</li>" for s in c["target_services"][:4])
        headline = {"candidate": "Recommended", "conditional": "Recommended when conditions are met"}[c["status"]]
        out.append(f'<div class="pathcard" style="border-top-color:{kit.PATHWAY_STATUS[c["status"]][1]}"><h3><span>{kit.esc(PATHWAYS[c["pathway"]])}</span><span>{kit.pathway_chip(c["status"])}</span></h3>'
                   f'<p><b>{kit.esc(headline)}.</b> {kit.esc(c["rationale"])}</p>' + (f'<ul>{services}</ul>' if services else "")
                   + f'<p class="small">Related findings <code>{kit.esc(", ".join(c["finding_ids"]) or "-")}</code></p></div>')
    legend = ('<p class="small"><b>Recommended</b> = the evidence supports it now; <b>Recommended when conditions are met</b> = worth doing once the conditions on the card hold. '
              'The order of work is left to the conversation with the workload owner.</p>')
    table = ""
    if rest:
        rows = "".join(f'<tr><td><b>{kit.esc(PATHWAYS[c["pathway"]])}</b></td><td>{kit.pathway_chip(c["status"])}</td>'
                       f'<td>{kit.esc(c["problem"] if c["status"] == "not_recommended" else c["rationale"])}'
                       + (f'<div class="small"><b>Before deciding:</b> {kit.esc(join_sentences(c["prerequisites"]))}</div>' if c["status"] == "not_assessed" and c.get("prerequisites") else "")
                       + '</td></tr>' for c in rest)
        table = (f'<h3>Not recommended now or not assessed yet ({len(rest)})</h3><p class="small"><b>Not recommended</b> = the evidence argues against it for this workload today; '
                 f'<b>Not assessed</b> = the evidence needed to judge it is missing, and the row says what would decide it.</p>'
                 f'<table><thead><tr><th>Direction</th><th>Status</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table>')
    return (('<div class="grid g3">' + "".join(out) + "</div>" + legend) if out else '<p class="small">No direction is recommended yet.</p>') + table


def load_specialists(run, checks, screener, manifest, inventory, prices):
    """05-modernization/specialist-advice.json from advice.py merge, re-checked (references resolve, wording rules hold),
    with each recommendation's list prices resolved like a key issue's."""
    import advice  # noqa: E402  (same folder)
    finding_ids = {c["id"] for c in checks + screener}
    evidence_ids = {c["id"] for c in manifest.get("calls", [])} | {e for c in checks + screener for e in c.get("evidence_ids") or []}
    resource_ids = {r["id"] for r in inventory.get("resources", [])}
    out = advice.load_for_report(run, finding_ids, evidence_ids, resource_ids, set(prices))
    for s in out:
        for rec in s.get("recommendations") or []:
            rec["list_prices"] = [dict(prices[r["id"]], sign=r["sign"]) for r in rec.get("list_prices") or []]
    if out:
        import benefits  # noqa: E402  (same folder)
        BENEFITS["result"] = benefits.compute(run, out)
        benefits.attach(out, BENEFITS["result"])
    return out


BENEFITS = {"result": None}  # the evidence rows computed for this render; written to 06-assessment/benefits.json


def ref_links(refs, finding_ids):
    out = []
    for r in refs or []:
        if r in finding_ids:
            out.append(f'<a href="#{kit.esc(r)}">{kit.esc(r)}</a>')
        elif re.fullmatch(r"[esh]\d{4,}", r):
            out.append(f'<a href="#ev-{kit.esc(r)}">{kit.esc(r)}</a>')
        else:
            out.append(f'<code>{kit.esc(r.split(":")[-1].split("/")[-1])}</code>')
    return " ".join(out)


def specialist_html(s, statuses, finding_ids, run, by_id=None):
    sid = s["specialist"]
    by_id = by_id or {}
    chips = "".join(kit.pathway_chip(statuses[p]) for p in s.get("pathways", []) if p in statuses)
    state = "".join(f'<li>{kit.esc(i["text"])} <span class="small">{ref_links(i.get("refs"), finding_ids)}</span></li>' for i in s.get("current_state") or [])
    later = "".join(f'<li>{kit.esc(i["text"])} <span class="small">{ref_links(i.get("refs"), finding_ids)}</span></li>' for i in s.get("not_now") or []) or '<li class="small">nothing set aside</li>'
    recs = [rec_fields_html(n, r, s, by_id, finding_ids) for n, r in enumerate(s.get("recommendations") or [], 1)]
    import advice  # noqa: E402  (same folder)
    handoffs = "".join(f'<li><b>{kit.esc(advice.SPECIALISTS.get(h["to"], {}).get("role", h["to"]))}</b> · {kit.esc(h["text"])}</li>' for h in s.get("handoffs") or [])
    diagram = s.get("diagram") or {}
    figure = ""
    svg_path = run / "05-modernization" / str(diagram.get("svg") or "")
    if diagram.get("svg") and svg_path.is_file():
        note = " · initial render (the final drawing was not produced)" if diagram.get("initial_render_only") else ""
        figure = zoom_figure(f"zoom-sp-{sid}", safe_svg(svg_path), f"sp-{sid}",
                             f'{kit.esc(diagram.get("caption", ""))}{kit.esc(note)} · click the diagram to enlarge · {open_full(svg_path, run)}', svg_path)
    refs = "".join(f'<li><a href="{kit.esc(r["url"])}" target="_blank" rel="noopener">{kit.esc(r["title"])}</a></li>' for r in s.get("references") or [])
    return (f'<article class="specialist" id="sp-{kit.esc(sid)}"><h3>{kit.esc(s["role"])} <span class="chips">{chips}</span></h3>'
            f'<p class="lead">{kit.esc(s["summary"])}</p>'
            f'<div class="grid g2"><div><h4>What we see now</h4><ul class="plain">{state}</ul></div><div><h4>Not now</h4><ul class="plain">{later}</ul></div></div>'
            + (f'<h4>Recommendations</h4>{"".join(recs)}' if recs else '<p class="small">No change recommended in this area for now; the reasons are under "Not now".</p>')
            + (f'<p class="small"><b>Depends on other areas</b></p><ul class="plain small">{handoffs}</ul>' if handoffs else "")
            + figure + (f'<div class="small"><b>References</b><ul>{refs}</ul></div>' if refs else "") + '</article>')


def pathway_table(pathways, specialists):
    owner = {p: s["role"] for s in specialists for p in s.get("pathways", [])}
    rows = "".join(f'<tr><td><b>{kit.esc(PATHWAYS[c["pathway"]])}</b></td><td>{kit.pathway_chip(c["status"])}</td><td class="small">{kit.esc(owner.get(c["pathway"], "-"))}</td>'
                   f'<td>{kit.esc(c["rationale"] if c["status"] in {"candidate", "conditional", "not_assessed"} else c["problem"])}</td></tr>' for c in pathways)
    return ('<table><thead><tr><th>Direction</th><th>Status</th><th>Reviewed by</th><th>Reason</th></tr></thead>'
            f'<tbody>{rows}</tbody></table><p class="small"><b>Recommended</b> = the evidence supports it now; <b>Recommended when conditions are met</b> = '
            'worth doing once the conditions hold; <b>Not recommended</b> = the evidence argues against it today; <b>Not assessed</b> = the evidence needed to judge '
            'it is missing. The order of work is left to the conversation with the workload owner.</p>')


def specialist_markdown(s, statuses, by_id=None):
    by_id = by_id or {}
    lines = [f"### {s['role']} ({', '.join(f'{PATHWAYS[p]}: {kit.PATHWAY_STATUS[statuses[p]][0]}' for p in s.get('pathways', []) if p in statuses)})", "", s["summary"], "",
             "**What we see now**", ""] + [f"- {i['text']} `{', '.join(i.get('refs') or [])}`" for i in s.get("current_state") or []] + [""]
    for n, r in enumerate(s.get("recommendations") or [], 1):
        lines += rec_fields_markdown(n, r, s, by_id)
    if s.get("not_now"):
        lines += ["**Not now**", ""] + [f"- {i['text']}" for i in s["not_now"]] + [""]
    if (s.get("diagram") or {}).get("svg"):
        lines += [f"Diagram: {s['diagram'].get('caption', '')} (`05-modernization/{s['diagram']['svg']}`)", ""]
    return lines


def join_sentences(items):
    """List items that are sentences already end in a period; join them with a space, not '; ' (which gave '.;')."""
    items = [str(i).strip() for i in items or [] if str(i).strip()]
    if not items:
        return "-"
    return " ".join(i if i.endswith((".", "!", "?")) else i + "." for i in items)  # the English draft; localization keeps sentence ends


def next_steps_html(steps, language="en"):
    """Services and assets once each (not once per pathway), with the pathways they serve, so a reader is not shown the
    same Compute Optimizer row three times."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import next_steps  # noqa: E402
    L = next_steps.labels(language)
    if not steps:
        return f'<p class="small">{kit.esc(L["none"])}</p>'
    names = {k: PATHWAY_LABELS(language).get(k, v) for k, v in PATHWAYS.items()}
    merged = next_steps.merged(steps, names, language)
    out = [f'<p class="lead">{kit.esc(L["lead"])}</p>']
    if merged["services"]:
        rows = "".join(f'<tr><td><a href="{kit.esc(s["url"])}" target="_blank" rel="noopener"><b>{kit.esc(s["name"])}</b></a><div class="small">{kit.esc(" · ".join(s["pathways"]))}</div></td>'
                       f'<td>{kit.esc(s.get("official_description", ""))}</td><td>{kit.esc(s["why"])}<div class="small">{kit.esc("; ".join(s["because_all"]))}</div></td>'
                       f'<td class="small"><a href="{kit.esc(s.get("source", ""))}" target="_blank" rel="noopener">{kit.esc(s.get("source", "").split("/")[2] if s.get("source") else "")}</a><br>{kit.esc(L["checked"])} {kit.esc(s.get("source_checked_at", ""))}</td></tr>'
                       for s in merged["services"].values())
        out.append(f'<h4>{kit.esc(L["services"])}</h4><table><thead><tr><th>{kit.esc(L["service"])}</th><th>{kit.esc(L["what"])}</th><th>{kit.esc(L["why"])}</th><th>{kit.esc(L["source"])}</th></tr></thead><tbody>{rows}</tbody></table>')
    if merged["assets"]:
        rows = "".join(f'<tr><td><a href="{kit.esc(a["url"])}" target="_blank" rel="noopener"><b>{kit.esc(a["name"])}</b></a><div class="small">{kit.esc(" · ".join(a["pathways"]))}</div></td><td>{kit.chip(L["kinds"].get(a["kind"], a["kind"]), kit.INK)}</td>'
                       f'<td>{kit.esc(a["why"])}<div class="small">{kit.esc("; ".join(a["because_all"]))}</div></td></tr>' for a in merged["assets"].values())
        out.append(f'<h4>{kit.esc(L["assets"])}</h4><table><thead><tr><th>{kit.esc(L["asset"])}</th><th>{kit.esc(L["kind"])}</th><th>{kit.esc(L["why"])}</th></tr></thead><tbody>{rows}</tbody></table>')
    return "".join(out)


def PATHWAY_LABELS(language):
    import next_steps  # noqa: E402  (sys.path set by next_steps_html)
    return next_steps.labels(language).get("pathways", PATHWAYS)


EVIDENCE_KIND_LABEL = {"configuration": "API configuration", "template_declared": "Template declaration", "source_declared": "Source declaration",
                       "documented": "Document", "measured": "Measurement", None: "API configuration"}


ACCOUNT_RULES = ("SECURITYHUB_ENABLED", "GUARDDUTY_ENABLED", "CONFIG_RECORDER_ENABLED", "INSPECTOR_ENABLED", "IAM_ACCESS_ANALYZER",
                 "COMPUTE_OPTIMIZER_ENROLLMENT", "TRUSTED_ADVISOR_ACCESS", "HEALTH_OPEN_EVENTS", "ORGANIZATIONS_MEMBERSHIP", "COST_EXPLORER_VISIBILITY", "IAC_COVERAGE",
                 "ROOT_MFA", "ROOT_ACCESS_KEYS")
ACCOUNT_RULE_LABEL = {"SECURITYHUB_ENABLED": "Security Hub", "GUARDDUTY_ENABLED": "GuardDuty", "CONFIG_RECORDER_ENABLED": "AWS Config recorder",
                      "INSPECTOR_ENABLED": "Amazon Inspector", "IAM_ACCESS_ANALYZER": "IAM Access Analyzer", "COMPUTE_OPTIMIZER_ENROLLMENT": "Compute Optimizer",
                      "TRUSTED_ADVISOR_ACCESS": "Trusted Advisor (support plan)", "HEALTH_OPEN_EVENTS": "AWS Health events", "ORGANIZATIONS_MEMBERSHIP": "Organizations",
                      "COST_EXPLORER_VISIBILITY": "Cost Explorer (account bill)", "IAC_COVERAGE": "Infrastructure-as-code coverage",
                      "ROOT_MFA": "Root user MFA", "ROOT_ACCESS_KEYS": "Root user access keys"}
COVERAGE_LABEL = {"collected": "collected", "supplement_when_in_scope": "collected per stack when in scope", "not_collected": "seen, not collected",
                  "not_enabled": "service not enabled / no support plan", "denied": "permission denied", "skipped": "skipped (budget)"}


ENABLEMENT = {  # state -> (label, colour, tint); neutral colours on purpose: an account service that is off is a choice to discuss, not a failure
    "enabled": ("Enabled", "#7AA116", "rgba(122,161,22,0.14)"),
    "partial": ("Partially enabled", "#EC7211", "rgba(236,114,17,0.14)"),
    "not_enabled": ("Not enabled", "#545B64", "rgba(84,91,100,0.12)"),
    "not_in_plan": ("Not in support plan", "#7D8998", "rgba(125,137,152,0.18)"),
    "unknown": ("Could not read", "#7D8998", "rgba(125,137,152,0.18)"),
    "observed": ("Observed", "#7D8998", "rgba(125,137,152,0.18)"),
}
# What each account-wide service is and what enabling it adds, for readers who do not know the service. The
# workload-specific part ("for this workload ...") is the finding's own recommendation, written by the rule.
ACCOUNT_SERVICE_INFO = {
    "SECURITYHUB_ENABLED": ("AWS Security Hub collects findings from GuardDuty, Inspector, IAM Access Analyzer, Config and partner tools into one view and evaluates the account "
                            "continuously against security standards such as the AWS Foundational Security Best Practices, CIS or PCI DSS.",
                            "One place to see and triage control failures across accounts and regions, a score per standard, and automation rules that route or suppress findings. "
                            "Standards need the AWS Config recorder."),
    "GUARDDUTY_ENABLED": ("Amazon GuardDuty is a managed threat detection service. It analyses CloudTrail events, VPC Flow Logs and DNS logs, and with protection plans also EKS audit "
                          "logs, runtime behaviour, RDS login activity and S3 access, for signs of compromise.",
                          "Findings such as credential misuse, crypto-mining or unusual API calls within minutes, without agents or changes to the workload."),
    "CONFIG_RECORDER_ENABLED": ("AWS Config records the configuration of resources over time and evaluates managed or custom rules against that history.",
                                "A timeline of what changed and when, drift detection against templates, compliance views, and the data Security Hub standards rely on. "
                                "Priced per configuration item recorded and per rule evaluation."),
    "INSPECTOR_ENABLED": ("Amazon Inspector scans EC2 instances (through the SSM agent), container images in Amazon ECR and Lambda functions for known software vulnerabilities and "
                          "unintended network exposure.",
                          "A prioritised list of packages to patch with a risk score, rescanned automatically when new CVEs are published. Priced per instance, image and function scanned."),
    "IAM_ACCESS_ANALYZER": ("IAM Access Analyzer reports resources whose policies grant access from outside the account or organization (S3 buckets, IAM roles, KMS keys, SQS queues, "
                            "Secrets Manager secrets, RDS snapshots and more) and can validate policies and find unused access.",
                            "A list of externally reachable resources the team can confirm or close; external-access analysis is free."),
    "COMPUTE_OPTIMIZER_ENROLLMENT": ("AWS Compute Optimizer analyses CloudWatch utilization of EC2 instances, Auto Scaling groups, EBS volumes, Lambda functions and ECS on Fargate "
                                     "and recommends sizes and types, including Graviton options.",
                                     "Right-sizing data that accumulates while the account runs normally; free and read-only."),
    "TRUSTED_ADVISOR_ACCESS": ("AWS Trusted Advisor compares the account against AWS best-practice checks for cost, performance, security, fault tolerance, service limits and operations.",
                               "The full check set and the API this assessment reads come with the Business and Enterprise support plans; Basic and Developer plans show a subset in the console."),
    "HEALTH_OPEN_EVENTS": ("AWS Health reports service events, planned changes and end-of-support notices for the resources in the account.",
                           "Deadlines for the roadmap (version retirements, scheduled maintenance); the API needs a Business or higher support plan."),
    "COST_EXPLORER_VISIBILITY": ("AWS Cost Explorer shows the account's bill by service, region, tag and time with month-over-month trends; it is free to enable.",
                                 "The answer to which service is driving a rising bill, and the baseline any right-sizing or Savings Plans decision needs."),
    "ORGANIZATIONS_MEMBERSHIP": ("AWS Organizations groups accounts under one management account with consolidated billing, service control policies and delegated administration "
                                 "of security services.",
                                 "Guardrails that apply to every account, central GuardDuty and Security Hub administration and simpler billing; easiest to set up before the second account exists."),
}


# What each account service contributes when the owner must show compliance (ISMS-P, PCI DSS, SOC 2 ...). The report
# never maps controls clause by clause; it says which service produces the continuous evidence auditors usually ask for.
COMPLIANCE_NOTE = {
    "SECURITYHUB_ENABLED": "Security Hub standards (CIS, PCI DSS, NIST, AWS Foundational) evaluate controls continuously and keep a history auditors can read; with obligations like these it is worth enabling.",
    "CONFIG_RECORDER_ENABLED": "A configuration history is the usual evidence that controls stayed in place between audits, and Security Hub standards need it; with obligations like these it is worth enabling.",
    "INSPECTOR_ENABLED": "Vulnerability scanning with a record of findings and fixes is a common audit request; with obligations like these it is worth enabling for EC2 and ECR.",
    "IAM_ACCESS_ANALYZER": "A list of externally shared resources answers the access-review questions in most frameworks; it is free and worth enabling.",
}


def enablement_chip(c):
    state = c.get("enablement")
    if state in ENABLEMENT:
        label, color, tint = ENABLEMENT[state]
        chips = kit.chip(label, color, tint)
        if c["status"] == "WARN" and state in {"not_enabled", "partial"}:
            chips += kit.chip("Consider", kit.ACCENT)
        return chips
    return kit.status_chip(c["status"])  # findings from a review older than 2.1.6 carry no enablement state


# Cost Explorer service names -> inventory type prefixes, to say whether a billed service exists in the assessed scope.
BILL_SERVICE_TYPES = {"Amazon Elastic Compute Cloud - Compute": ("ec2",), "EC2 - Other": ("ec2",), "Amazon Virtual Private Cloud": ("ec2",),
                      "Amazon Relational Database Service": ("rds",), "Amazon ElastiCache": ("elasticache",), "Amazon Elastic Container Service": ("ecs",),
                      "Amazon Elastic Container Service for Kubernetes": ("eks",), "Amazon Elastic Kubernetes Service": ("eks",), "Elastic Load Balancing": ("elbv2", "elb"),
                      "Amazon OpenSearch Service": ("opensearch",), "Amazon Neptune": ("neptune",), "Amazon Bedrock": ("bedrock",), "AWS Lambda": ("lambda",),
                      "Amazon Simple Storage Service": ("s3",), "Amazon DynamoDB": ("dynamodb",), "AmazonCloudWatch": ("cloudwatch", "logs"), "Amazon EC2 Container Registry (ECR)": ("ecr",),
                      "AWS Secrets Manager": ("secretsmanager",), "Amazon Simple Queue Service": ("sqs",), "Amazon Simple Notification Service": ("sns",),
                      "AWS Key Management Service": ("kms",), "Amazon Route 53": ("route53",), "Amazon CloudFront": ("cloudfront",), "AWS CodePipeline": ("codepipeline",),
                      "CodeBuild": ("codebuild",), "AWS WAF": ("wafv2",), "Amazon GuardDuty": ("guardduty",), "AWS Config": ("config",), "Amazon Q": ("q",)}


def spend_html(inventory, scope_ids=None):
    """The account bill for the last closed months as Cost Explorer reported it: real figures, account-wide, not attributed
    to the workload. Rendered only when the collector read them."""
    bill = next((r for r in (inventory or {}).get("resources", []) if r.get("type") == "ce.cost-by-service" and (r.get("collection") or {}).get("status", "ok") == "ok"), None)
    months = (bill or {}).get("attributes", {}).get("months") or []
    if not months:
        return ""
    head = "".join(f"<th>{kit.esc(str(m.get('start', ''))[:7])}</th>" for m in months)
    totals = "".join(f"<td>{m.get('total_usd', 0):,.0f}</td>" for m in months)
    names = []
    for m in months:
        for s in (m.get("services") or [])[:6]:
            if s["service"] not in names:
                names.append(s["service"])
    by_month = [{s["service"]: s["usd"] for s in (m.get("services") or [])} for m in months]
    scoped_types = {r["type"].split(".")[0] for r in (inventory or {}).get("resources", []) if r.get("level") != "account" and r.get("in_scope", True)}
    if scope_ids is not None:
        scoped_types = {r["type"].split(".")[0] for r in (inventory or {}).get("resources", []) if r.get("id") in scope_ids}

    def in_workload(service):
        prefixes = BILL_SERVICE_TYPES.get(service)
        if prefixes is None:
            return "-"
        return "yes" if any(p in scoped_types for p in prefixes) else "no"
    rows = "".join(f"<tr><td>{kit.esc(n)}</td><td class=\"small\">{kit.esc(in_workload(n))}</td>" + "".join(f"<td>{bm.get(n, 0):,.0f}</td>" for bm in by_month) + "</tr>" for n in names[:10])
    share = bill_share(months[-1], in_workload)
    return (f'<h3>Account bill, last {len(months)} closed months</h3><p class="small">Unblended cost in USD as Cost Explorer reports it for the whole account. The workload\'s share is not '
            f'separated (cost allocation tags are a payer-account setting), so read this as the account trend and the services behind it, not as the workload\'s cost. '
            f'"In this workload" says whether the assessed scope contains resources of that service at all; "no" means the cost comes from other workloads in the account.</p>'
            f'<table><thead><tr><th>Service</th><th>In this workload</th>{head}</tr></thead><tbody><tr><td><b>Account total</b></td><td></td>{totals}</tr>{rows}</tbody></table>{share}')


def bill_share(month, in_workload):
    """The billed amount of the services the workload uses at all, for the latest month: a ceiling for the workload's own
    share among the largest services, since other workloads may use the same services. Real figures, added, no estimate."""
    services = month.get("services") or []
    used = [s for s in services if in_workload(s["service"]) == "yes"]
    if not used:
        return ""
    amount = sum(s["usd"] for s in used)
    return (f'<p class="small"><b>Services this workload uses:</b> ${amount:,.0f} of the ${month.get("total_usd", 0):,.0f} account total in {kit.esc(str(month.get("start", ""))[:7])} '
            f'({len(used)} of the {len(services)} largest services). Other workloads may use the same services, so this is the most the workload could account for among them, '
            f'not its cost.</p>')


def account_posture_html(checks, manifest, inventory=None, scope_context=None, scope_ids=None):
    """Account-level services (one row per check with its enablement state), a card per service worth considering, the
    account bill when Cost Explorer answered, and the discovery-based query-scope table."""
    rows, consider = [], []
    for rule in ACCOUNT_RULES:
        for c in sorted((x for x in checks if x["rule_id"] == rule), key=lambda x: x.get("target") or ""):
            rows.append(f'<tr><td>{kit.esc(ACCOUNT_RULE_LABEL[rule])}</td><td><code>{kit.esc(target_of(c))}</code></td><td>{enablement_chip(c)}</td>'
                        f'<td>{kit.esc(c["observation"])}</td><td><a href="#{kit.esc(c["id"])}">{kit.esc(c["id"])}</a></td></tr>')
            if c["status"] == "WARN" and c.get("enablement") in {"not_enabled", "partial"} and rule in ACCOUNT_SERVICE_INFO:
                consider.append(c)
    posture = ('<table><thead><tr><th>Service</th><th>Where</th><th>Status</th><th>What we saw</th><th>Ref</th></tr></thead><tbody>'
               + ("".join(rows) or '<tr><td colspan="5" class="small">Account-level services were not collected in this snapshot (collector older than 2.1.5).</td></tr>') + "</tbody></table>")
    cards = ""
    if consider:
        items = []
        for c in consider:
            what, adds = ACCOUNT_SERVICE_INFO[c["rule_id"]]
            doc = (c.get("source_urls") or [""])[0]
            obligations = [x for x in (scope_context or {}).get("compliance", []) if x not in ("unknown", "none")]
            for_compliance = (f'<p><b>For the stated obligations</b> The scope interview names {kit.esc(", ".join(obligations))}. {kit.esc(COMPLIANCE_NOTE[c["rule_id"]])}</p>'
                              if obligations and c["rule_id"] in COMPLIANCE_NOTE else "")
            items.append(f'<article class="card consider"><h4>{kit.esc(ACCOUNT_RULE_LABEL[c["rule_id"]])} {enablement_chip(c)}'
                         f'{kit.chip("Recommended for compliance", kit.ACCENT) if for_compliance else ""}</h4>'
                         f'<p><b>What it is</b> {kit.esc(what)}</p><p><b>What enabling adds</b> {kit.esc(adds)}</p>'
                         f'<p><b>For this workload</b> {kit.esc(c["recommendation"])}</p>{for_compliance}'
                         f'<p class="small">Finding <a href="#{kit.esc(c["id"])}">{kit.esc(c["id"])}</a>'
                         + (f' · <a href="{kit.esc(doc)}" target="_blank" rel="noopener">Documentation</a>' if doc else "") + '</p></article>')
        cards = (f'<h3>Services worth considering</h3><p class="small">Each service below is off or only partly on. None of them is required: whether it earns its place depends '
                 f'on the workload\'s requirements, compliance obligations and the tooling the team already runs. The notes say what the service does, what turning it on would '
                 f'add here, and when leaving it off is a reasonable choice.</p><div class="grid g2">{"".join(items)}</div>')
    support = next((c for c in checks if c["rule_id"] == "TRUSTED_ADVISOR_ACCESS"), None)
    plan_note = ""
    known_plan = (scope_context or {}).get("support_plan", "unknown")
    if support and support["status"] == "UNKNOWN" and "subscription required" in support["observation"].lower():
        if known_plan not in ("unknown", "", None):
            plan_note = (f'<div class="callout"><p><b>Support plan.</b> The scope interview names the {kit.esc(known_plan)} plan for this account, and the Trusted Advisor and Health APIs answered '
                         f'"subscription required" here, so this report contains no Trusted Advisor checks or Health notices. The AWS Health Dashboard in the console still shows planned changes '
                         f'and end-of-support notices on every plan.</p></div>')
        else:
            plan_note = ('<div class="callout"><p><b>Support plan.</b> The Trusted Advisor and Health APIs answered "subscription required", which points to the Basic or '
                         'Developer plan for this account (the owner question below asks for the actual plan), so this report contains no Trusted Advisor checks or Health notices of planned '
                         'changes and end-of-support dates. The AWS Health Dashboard in the console still shows those notices on every plan; only the API needs Business or higher.</p></div>')
    coverage = manifest.get("service_coverage") or {}
    discovery = manifest.get("discovery") or {}
    totals = sum((d.get("tagging") or {}).get("total", 0) for d in discovery.values())
    if coverage:
        order = {"collected": 0, "supplement_when_in_scope": 1, "not_enabled": 2, "denied": 3, "skipped": 4, "not_collected": 5}
        crow = "".join(f'<tr><td><code>{kit.esc(svc)}</code></td><td>{kit.num(info.get("discovered_resources", 0))}</td>'
                       f'<td>{kit.esc(COVERAGE_LABEL.get(info.get("status"), info.get("status")))}{" · added to the query scope by discovery" if info.get("auto_enabled") else ""}</td></tr>'
                       for svc, info in sorted(coverage.items(), key=lambda kv: (order.get(kv[1].get("status"), 9), -kv[1].get("discovered_resources", 0), kv[0])))
        auto = manifest.get("auto_enabled_services") or []
        scope_table = (f'<p class="small">Discovery listed {totals} tagged or once-tagged resources across {len(discovery)} region(s) through the Resource Groups Tagging API '
                       f'(ARNs and tag keys only; never-tagged resources are not counted). Services found in the account that the default scan does not cover were added to the query scope automatically (nothing was changed in the account)'
                       f'{": " + kit.esc(", ".join(auto)) if auto else " (none needed)"}. "Seen, not collected" means the service exists in the account but this assessment has no collector or rule for it; '
                       f'it is listed so the gap is visible rather than silent.</p>'
                       f'<details><summary class="small">Query scope by service ({len(coverage)})</summary><div class="body"><table><thead><tr><th>Service (ARN name)</th><th>Discovered</th><th>In this assessment</th></tr></thead><tbody>{crow}</tbody></table></div></details>')
    else:
        scope_table = '<p class="small">No discovery data in this snapshot.</p>'
    return (f'<p class="lead">Account-wide services that protect or observe every workload in the account. The table shows whether each one is on; it is not a pass or fail '
            f'list. Whether a service is worth enabling depends on the workload\'s requirements, existing tooling and compliance needs, so each one that is off comes with a note '
            f'on what it does and when it tends to matter. The second part shows what the discovery scan found in the account and whether this assessment looked at it.</p>'
            f'{posture}{plan_note}{compliance_callout(scope_context, (inventory or {}).get("regions"))}{cards}{spend_html(inventory, scope_ids)}<h3>Query scope</h3>{scope_table}')


def evidence_index(manifest, checks, screener, specialists=None):
    """One row per evidence ID that the report cites: what was asked, of what, when, and which findings (or Specialist
    SA Agent recommendations) rely on it.

    IDs: e#### = base inventory call, s#### = supplementary workload evidence (templates, policies, source files),
    h#### = host inspection. Raw payloads stay in the run directory; the report shows the call, not the content."""
    calls = {c["id"]: c for c in manifest.get("calls", [])}
    used = {}
    for c in checks + screener:
        cited = list(c.get("evidence_ids", []) or []) + re.findall(r"\b([esh]\d{4,})\b", f"{c.get('observation', '')} {c.get('recommendation', '')}")
        for e in dict.fromkeys(cited):  # IDs quoted in the text of a manual check are evidence too
            used.setdefault(e, []).append(c["id"])
    for sp in specialists or []:
        items = [("current", i) for i in sp.get("current_state") or []] + [("rec", r) for r in sp.get("recommendations") or []] + [("later", i) for i in sp.get("not_now") or []]
        for _, item in items:
            for ref in item.get("refs") or []:
                if re.fullmatch(r"[esh]\d{4,}", ref) and sp["specialist"] not in used.setdefault(ref, []):
                    used[ref].append(sp["specialist"])
    rows = []
    for eid in sorted(used, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0)):
        call = calls.get(eid)
        if call is None:
            rows.append({"id": eid, "what": "not in the collection manifest (imported or host evidence)", "target": "-", "kind": "-", "when": "-", "status": "-", "findings": used[eid], "raw": "-"})
            continue
        args = call.get("arguments") or []
        target = " ".join(str(a) for a in args if not str(a).startswith("--")) or ("account/region" if not args else " ".join(str(a) for a in args))
        if call.get("provenance") and call["provenance"].get("locator"):
            target = (target + " · " if target != "account/region" else "") + str(call["provenance"]["locator"])
        rows.append({"id": eid, "what": f"aws {call.get('service')} {call.get('operation')}", "target": target[:160],
                     "kind": EVIDENCE_KIND_LABEL.get(call.get("evidence_kind"), str(call.get("evidence_kind"))),
                     "when": str(call.get("collected_at") or "")[:16].replace("T", " "), "status": call.get("status", "-"),
                     "findings": used[eid], "raw": call.get("raw_path") or "-"})
    return rows


def evidence_html(rows):
    if not rows:
        return '<p class="small">No evidence cited.</p>'
    return (f'<p class="lead">{kit.esc(CODES_SENTENCE)} Every finding cites the evidence it was judged on; the table below lists each evidence ID with the read-only call behind it.</p>'
            f'<details><summary>Evidence table ({len(rows)} item(s))</summary><div class="body">{evidence_table(rows)}</div></details>')


def used_by_link(ref):
    """A finding id links to its card; a specialist id to its section (the role name is the label)."""
    import advice  # noqa: E402  (same folder)
    if ref in advice.SPECIALISTS:
        return f'<a href="#sp-{kit.esc(ref)}">{kit.esc(advice.SPECIALISTS[ref]["role"].replace(" Specialist SA Agent", ""))}</a>'
    return f'<a href="#{kit.esc(ref)}">{kit.esc(ref)}</a>'


def evidence_table(rows):
    body = "".join(f'<tr id="ev-{kit.esc(r["id"])}"><td><code>{kit.esc(r["id"])}</code></td><td><code>{kit.esc(r["what"])}</code></td><td>{kit.esc(r["target"])}</td>'
                   f'<td>{kit.esc(r["kind"])}</td><td class="small">{kit.esc(r["when"])}</td><td class="small">{"ok" if r["status"] == "ok" else kit.esc(r["status"])}</td>'
                   f'<td class="small">{" ".join(used_by_link(f) for f in r["findings"][:8])}{" +" + str(len(r["findings"]) - 8) if len(r["findings"]) > 8 else ""}</td>'
                   f'<td class="small"><code>{kit.esc(r["raw"])}</code></td></tr>' for r in rows)
    return ('<p class="small">The raw response of each call is stored under the run directory at the path shown; the report never reproduces its content.</p>'
            '<table class="evidence-table"><thead><tr><th>ID</th><th>Call</th><th>Target / locator</th><th>Kind</th><th>Collected (UTC)</th><th>Result</th><th>Used by</th><th>Raw file</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def method_appendix(scores, checks, pathways, weights):
    counts = Counter(c["status"] for c in checks)
    criteria = {"business_value": "Business value", "technical_fit": "Technical fit", "operating_model": "Operating model", "risk_manageability": "Risk manageability",
                "delivery_feasibility": "Delivery feasibility", "cost_confidence": "Cost confidence"}
    heat = '<table><thead><tr><th>Direction</th>' + "".join(f'<th style="text-align:center">{kit.esc(v)} {weights[k]}</th>' for k, v in criteria.items()) + '<th>Verified weight</th></tr></thead><tbody>'
    for c in pathways:
        heat += f'<tr><td>{kit.esc(PATHWAYS[c["pathway"]])}</td>' + "".join(kit.heat_cell(c["criteria"][k]["score"]) for k in criteria) + f'<td style="text-align:center">{c["known_weight_percent"]}%</td></tr>'
    heat += "</tbody></table>"
    return (f'<details><summary>How the grades are derived</summary><div class="body">{kit.grade_rubric_html()}</div></details>'
            f'<details><summary>Numeric scores (for pathway ranking)</summary><div class="body"><p class="small">Local score = weighted average of PASS 1, WARN 0.5, FAIL 0 over judged checks; UNKNOWN is excluded and only lowers evidence coverage. These are inputs to pathway ranking, not a rating of the workload.</p>'
            f'{kit.score_bars(scores)}<div class="grid g2" style="margin-top:12px"><div class="card"><h3>Judgement distribution</h3>{kit.stacked_status(scores)}{kit.legend([(kit.STATUS[k][0], kit.STATUS[k][1]) for k in kit.STATUS_ORDER])}</div>'
            f'<div class="card"><h3>Fail and warn by severity</h3>{kit.severity_bars(checks)}</div></div>'
            f'<h4>Pathway criteria (1-5, "-" = unknown)</h4>{heat}<p class="small">A total score and rank appear only when all six criteria are verified; unknown criteria stay unknown rather than becoming zero.</p></div></details>')



CREATED_KEYS = ("ClusterCreateTime", "InstanceCreateTime", "createdAt", "CreatedAt", "CreationDate", "CreatedTime", "CreateTime", "CreationTime", "LaunchTime", "CreatedDate")


def resource_tags(r):
    """Tags as {key: value} from the shapes AWS APIs return (Tags/TagList/TagSet lists of Key/Value or key/value, or a map)."""
    a = r.get("attributes") or {}
    for key in ("Tags", "tags", "TagList", "TagSet"):
        value = a.get(key)
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        if isinstance(value, list):
            return {str(x.get("Key", x.get("key"))): str(x.get("Value", x.get("value"))) for x in value if isinstance(x, dict) and (x.get("Key") or x.get("key"))}
    return {}


def environment_facts(inventory, scope_ids, context):
    """(oldest date, age in days, dated resources, non-production tags) when the scope interview says production but the
    resources look new or carry test tags; None otherwise. Facts only: creation dates and tag values found."""
    if (context or {}).get("environment") != "production":
        return None
    dates = []
    for r in (inventory or {}).get("resources", []):
        if r.get("level") == "account" or (scope_ids is not None and r.get("id") not in scope_ids):
            continue
        for key in CREATED_KEYS:
            value = (r.get("attributes") or {}).get(key)
            if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
                dates.append(value[:10])
                break
    if len(dates) < 3:
        return None
    oldest = min(dates)
    try:
        age = (datetime.now(timezone.utc).date() - datetime.strptime(oldest, "%Y-%m-%d").date()).days
    except ValueError:
        return None
    tags = Counter()
    for r in (inventory or {}).get("resources", []):
        if r.get("level") == "account" or (scope_ids is not None and r.get("id") not in scope_ids):
            continue
        for k, v in resource_tags(r).items():
            if re.search(r"(?i)\b(test|testbed|dev|staging|stage|sandbox|poc|demo|qa)\b|testbed", f"{v}") and not str(k).startswith("aws:"):
                tags[f"{k}={v}"] += 1
    if age >= 30 and not tags:
        return None
    return {"oldest": oldest, "age": age, "dated": len(dates), "tags": tags.most_common(2)}


def environment_note(inventory, scope_ids, context):
    """When the scope interview says production but every dated resource in scope is younger than 30 days (or tags name a
    test purpose), say so: the reader should confirm before reading severities as production risk."""
    facts = environment_facts(inventory, scope_ids, context)
    if not facts:
        return ""
    tag_text = (" Tags on the resources also name a non-production purpose: " + ", ".join(f"{k} ({n})" for k, n in facts["tags"]) + ".") if facts["tags"] else ""
    return (f'<div class="callout"><p><b>Environment.</b> The scope interview names this workload production. The oldest dated resource in scope was created on {kit.esc(facts["oldest"])} '
            f'({facts["age"]} days ago, from {facts["dated"]} dated resources).{kit.esc(tag_text)} Worth confirming before the meeting; if the workload is not serving customers yet, read the issues below '
            f'as items to have in place before launch.</p></div>')


# Short definitions for terms readers at a beginner-to-intermediate level asked about. Only the terms that occur in
# this report are listed; the wording is the English draft and is localized with the rest of the report.
TERMS = {
    "RTO": "Recovery time objective: how long the service may be down after a failure before it hurts the business.",
    "RPO": "Recovery point objective: how much recent data the business can afford to lose after a failure.",
    "SLO": "Service level objective: a target the team sets for itself, such as 99.9% of requests answered within one second.",
    "Multi-AZ": "Running copies in two or more Availability Zones (separate data centres in one region) so one zone's failure does not stop the service.",
    "IaC": "Infrastructure as code: templates (CloudFormation, CDK, Terraform) that create the resources, so changes are reviewed like code.",
    "drift": "A difference between a stack's template and the resources as they are now, usually from a change made outside the template.",
    "DeletionPolicy": "A CloudFormation setting that decides what happens to a resource when its stack is deleted: delete it, keep it, or snapshot it first.",
    "IMDSv2": "The session-based version of the EC2 instance metadata service, which blocks a common way of stealing instance credentials.",
    "least privilege": "Giving a role only the actions and resources it actually uses.",
    "circuit breaker": "An ECS deployment setting that stops a failing deployment and rolls back to the last working version automatically.",
    "PodDisruptionBudget": "A Kubernetes rule that keeps a minimum number of pods running while nodes are drained or upgraded.",
    "HPA": "Horizontal Pod Autoscaler: Kubernetes adds or removes pods based on load.",
    "Karpenter": "An open-source node autoscaler for Kubernetes that starts right-sized nodes when pods need them.",
    "EKS Auto Mode": "An EKS mode in which AWS runs the worker nodes, their scaling and their upgrades for the cluster.",
    "RDS Proxy": "A managed connection pool in front of RDS and Aurora that lets many application instances share a few database connections.",
    "condition key": "A part of an IAM policy that allows an action only when the request meets a condition, such as naming a specific Bedrock guardrail.",
    "Spot": "Spare EC2 capacity at a lower price that AWS can take back with a 2-minute notice; suited to work that can restart.",
    "zero-ETL": "An AWS integration that copies operational data into an analytics service continuously without a pipeline the team has to run.",
    "Graviton": "AWS-designed Arm processors; the same workload usually costs less per unit of performance on them.",
    "Savings Plans": "A one- or three-year spend commitment that lowers the hourly price of compute.",
    "guardrail": "In Amazon Bedrock, a policy that filters harmful or off-topic prompts and answers.",
    "Pod Identity": "An EKS feature that gives each pod its own IAM role instead of sharing the node's role.",
    "extended support": "Paid support for a Kubernetes or database version after standard support ends; billed per hour on top of the normal price.",
    "Performance Insights": "An RDS and Aurora feature that shows which queries and waits use the database's capacity.",
    "Container Insights": "CloudWatch metrics and logs for ECS and EKS clusters, services and tasks.",
    "Specialist SA Agent": "An AI agent that takes the role of an AWS specialist solutions architect for one technology area (containers, databases, analytics, generative AI, DevOps); its advice is a draft the assessing architect confirms.",
}


def terms_html(text):
    found = [(term, meaning) for term, meaning in TERMS.items() if re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, re.I)]
    if not found:
        return '<p class="small">No specialised terms needed explaining.</p>'
    rows = "".join(f'<tr><td><b>{kit.esc(term)}</b></td><td>{kit.esc(meaning)}</td></tr>' for term, meaning in found)
    return f'<table><thead><tr><th>Term</th><th>Meaning</th></tr></thead><tbody>{rows}</tbody></table>'


# Minutes from the start of the run to the first rendering of the report. A re-render (`moda-skill report`, a label fix)
# keeps the first figure, read back from assessment-summary.json, instead of counting the hours since.
WALL_CLOCK = {"minutes": None}


def run_minutes(inventory):
    """Minutes from the run's start (its id is the UTC start time) to the newest file in the stage folders, so a report
    rendered early, re-rendered later or localized shows the same length of run."""
    if WALL_CLOCK["minutes"] is not None:
        return WALL_CLOCK["minutes"]
    try:
        started = datetime.strptime(str(inventory.get("run_id", ""))[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = WALL_CLOCK.get("end") or datetime.now(timezone.utc)
    return max(0, int((end - started).total_seconds() // 60))


def last_stage_write(run):
    """Time of the newest file in the stage folders 01- to 05-, or None. Report folders, the usage record and files written
    after the assessment (a later write audit, follow-up commands) do not move the end."""
    newest = None
    for path in run.rglob("*"):
        relative = path.relative_to(run).parts
        if not path.is_file() or not re.match(r"0[1-5]-", relative[0]):
            continue
        mtime = path.stat().st_mtime
        newest = mtime if newest is None or mtime > newest else newest
    return datetime.fromtimestamp(newest, timezone.utc) if newest is not None else None


def usage_withheld(run_meta, inventory):
    """True when the recorded AI usage is far below the run's wall-clock: a client that records a turn when it ends cannot
    report the turn that is still writing this report, so the figures describe the first minutes, not the run."""
    minutes = run_minutes(inventory)
    recorded = ((run_meta or {}).get("totals") or {}).get("inference_seconds")
    return bool(run_meta and minutes and recorded is not None and recorded < 0.25 * minutes * 60)


def usage_html(run_meta, inventory):
    """AI usage figures only when they describe the run (see usage_withheld)."""
    if usage_withheld(run_meta, inventory):
        recorded = run_meta["totals"]["inference_seconds"]
        models = ", ".join((run_meta or {}).get("models_seen") or []) or "not recorded"
        return (f'<p class="small"><b>AI assistant usage:</b> model {kit.esc(models)}. The client records usage when a turn ends, and most of this run happened in the turn that '
                f'is writing this report, so the recorded figures ({kit.fmt_seconds(recorded)}) describe only the first minutes and are not shown as the run\'s usage.</p>')
    return (f'<details><summary class="small">AI assistant usage recorded for this run (completed turns only)</summary><div class="body">{kit.run_metadata_html(run_meta)}</div></details>')


def usage_markdown(run_meta, inventory):
    """Markdown twin of run_clock_html + usage_html: the wall-clock first, and the usage table only when it describes the run."""
    minutes = run_minutes(inventory)
    clock = [f"- Run wall-clock: {minutes // 60} h {minutes % 60} min from the start of setup to this report, including collection, review and any pauses"] if minutes else []
    if usage_withheld(run_meta, inventory):
        models = ", ".join(run_meta.get("models_seen") or []) or "not recorded"
        return (["## Run provenance", "", f"- LLM model: {models}"] + clock
                + ["- AI assistant usage: the client records usage when a turn ends, and most of this run happened in the turn that wrote this report, "
                   "so the recorded figures describe only the first minutes and are not shown as the run's usage.", ""])
    lines = kit.run_metadata_markdown(run_meta)
    return lines[:4] + clock + lines[4:] if clock else lines


def write_audit_text(audit):
    """What CloudTrail shows about changes by the assessment's principal (write_audit.py), in plain text, so the reader
    can see that the run stayed read-only rather than take it on trust."""
    if not audit:
        return "not run. write_audit.py checks CloudTrail for changes made by the assessment's principal during the run."
    regions = ", ".join(sorted(audit.get("regions") or {}))
    window = audit.get("window") or {}
    if audit.get("status") == "clean":
        expected = len(audit.get("expected") or [])
        extra = f" {expected} SSM SendCommand call(s) for the host inspection are the expected exception." if expected else ""
        text = f"CloudTrail shows no change by the assessment's principal from {window.get('start', '')} to {window.get('end', '')} in {regions}.{extra}"
    elif audit.get("status") == "writes_found":
        items = "; ".join(f"{e.get('time', '')} {e.get('source', '')} {e.get('name', '')} ({e.get('region', '')})" for e in audit.get("unexpected", [])[:20])
        tools = ", ".join(f"{tool} ({count})" for tool, count in list((audit.get("unexpected_by_tool") or {}).items())[:5])
        by_tool = (f" Calls by tool: {tools}. Other work done with the same principal during the run, such as a deployment "
                   "from another session, appears here too; a profile used only for the assessment keeps this check clear.") if tools else ""
        failed = ", ".join(f"{r} ({v.get('error_code') or 'error'})" for r, v in (audit.get("regions") or {}).items() if v.get("status") != "ok")
        partial = f" Not every event could be read: {failed}." if failed else ""
        text = (f"CloudTrail shows {len(audit.get('unexpected', []))} change(s) by the assessment's principal during the run: {items}.{by_tool}{partial} "
                "Review them before this report is used.")
    else:
        failed = ", ".join(f"{r} ({v.get('error_code') or 'error'})" for r, v in (audit.get("regions") or {}).items() if v.get("status") != "ok")
        text = f"the CloudTrail check could not read every region: {failed}. No change was found in the regions it read."
    return f"{text} {audit.get('note', '')}".strip()


def write_audit_html(audit):
    return f'<p class="small"><b>Write audit:</b> {kit.esc(write_audit_text(audit))}</p>'


def write_audit_markdown(audit):
    return [f"- Write audit: {write_audit_text(audit)}", ""]


def run_clock_html(inventory):
    """Wall-clock time from the start of the run (its id is the UTC start time) to this report, so the assistant's own
    usage figures below are not read as the length of the assessment."""
    minutes = run_minutes(inventory)
    if not minutes:
        return ""
    return (f'<p class="small"><b>Run wall-clock:</b> {minutes // 60} h {minutes % 60} min from the start of setup to this report, including collection, '
            f'review, the architect\'s checks and any pauses. The AI-usage figures below cover only the assistant turns completed before this report was written.</p>')


TO_BE_STATES = {"proposed": "New", "changed": "Configuration change", "removal_candidate": "Replace, then retire", "unresolved": "Decision pending"}


def to_be_changes_html(diagram_path):
    """The to-be diagram's changes as a short table (new, changed, retired, pending), read from the graph the diagram was
    drawn from, so the target is readable without zooming the picture."""
    if not diagram_path:
        return ""
    graph_path = Path(diagram_path).parent / "diagram-graph.json"
    if not graph_path.is_file():
        return ""
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    rows = []
    for state, label in TO_BE_STATES.items():
        nodes = [n for n in graph.get("nodes", []) if n.get("state") == state]
        for n in nodes:
            rows.append(f'<tr><td>{kit.esc(label)}</td><td><b>{kit.esc(n.get("label", n.get("id")))}</b></td><td class="small">{kit.esc(str(n.get("detail") or "")[:220])}</td></tr>')
    if not rows:
        return ""
    kept = sum(1 for n in graph.get("nodes", []) if n.get("state") == "retained")
    return (f'<details open><summary class="small">What changes in the target ({len(rows)} item(s); {kept} component(s) kept as they are)</summary><div class="body">'
            f'<table><thead><tr><th>Change</th><th>Component</th><th>Detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def open_full(diagram_path, run):
    """A link to the SVG file next to the report (relative to the run directory) so small labels can be zoomed in a browser tab."""
    try:
        rel = os.path.relpath(Path(diagram_path).resolve(), (run / "06-assessment").resolve())
    except (ValueError, OSError):
        return ""
    return f'<a href="{kit.esc(rel)}" target="_blank" rel="noopener">open the SVG full size</a>'


def resolve_as_is(run, findings):
    """Use the snapshot the review actually judged (recorded by review.py as as_is_snapshot).

    Older findings.json files lack the pointer; fall back to the enriched snapshot, then the base one.
    Reading a different snapshot than the review would make resource counts and evidence ids disagree."""
    pointer = (findings.get("as_is_snapshot") or {}).get("path")
    if pointer:
        candidate = (run / pointer).resolve()
        if candidate.is_dir() and (candidate / "inventory.json").is_file():
            return candidate
        raise ValueError(f"findings.json points to as-is snapshot {pointer!r} which is missing")
    return run / "03-as-is-enriched" if (run / "03-as-is-enriched").is_dir() else run / "03-as-is"


# What the reference codes mean, said once near the top and again above the evidence table.
CODES_SENTENCE = ("Reference codes: f0001 = a finding from an automated check (f, then its number); m0001 = a finding written during the assessment from "
                  "templates, policies and source code; e0001 = the inventory API call it rests on; s0001 = supplementary evidence (stack templates, IAM "
                  "policies, task definitions, source files); h0001 = host inspection; p001 = a published list price. The codes link to the item.")


def render_inputs(svg_path):
    """base64 of <name>.render.json beside a drawing made by aws_diagram.py, or "" (a hand-made drawing)."""
    if not svg_path:
        return ""
    path = Path(svg_path)
    stem = path.name[:-len(".aws.svg")] if path.name.endswith(".aws.svg") else path.stem
    inputs = path.with_name(f"{stem}.render.json")
    if not inputs.is_file():
        return ""
    import base64
    return base64.b64encode(inputs.read_bytes()).decode()


def zoom_figure(fig_id, svg, back, caption, svg_path=None):
    """A diagram that opens full screen when clicked and closes with the button, with CSS :target only (the report carries
    no script). The enlarged view is twice the page width and scrolls. A drawing made by aws_diagram.py carries its render
    inputs, so a translated report redraws it from translated labels."""
    data = render_inputs(svg_path)
    attr = f' data-render="{data}"' if data else ""
    return (f'<figure class="zoomable" id="{kit.esc(fig_id)}"{attr}><a class="zoom-open" href="#{kit.esc(fig_id)}" title="Click to enlarge">{svg}</a>'
            f'<a class="zoom-close" href="#{kit.esc(back)}">Close</a><figcaption>{caption}</figcaption></figure>')


def scope_sentence(workload, inventory, scope, scope_ids, scope_context):
    """What was assessed, in one paragraph: workload, account, regions, stacks and resource count, the share of the
    account bill its services could account for, and what the scope interview settled."""
    stacks = ((scope or {}).get("workload") or {}).get("stack_names") or []
    count = len(scope_ids) if scope_ids is not None else len([r for r in inventory.get("resources", []) if r.get("level") != "account"])
    where = ", ".join(inventory.get("regions") or [])
    text = (f"The {workload} workload in account {inventory.get('account_id')} ({where})"
            + (f": {len(stacks)} CloudFormation stack(s), {count} resources assessed." if stacks else f": {count} resources assessed.")
            + " Other workloads in the account were not assessed.")
    bill = next((r for r in inventory.get("resources", []) if r.get("type") == "ce.cost-by-service" and (r.get("collection") or {}).get("status", "ok") == "ok"), None)
    months = (bill or {}).get("attributes", {}).get("months") or []
    if months:
        last = months[-1]
        scoped_types = {r["type"].split(".")[0] for r in inventory.get("resources", []) if (scope_ids is None and r.get("level") != "account") or (scope_ids is not None and r.get("id") in scope_ids)}
        used = sum(s["usd"] for s in last.get("services") or [] if any(p in scoped_types for p in BILL_SERVICE_TYPES.get(s["service"], ())))
        text += (f" The account bill for {str(last.get('start', ''))[:7]} was ${last.get('total_usd', 0):,.0f} for every workload in the account"
                 + (f"; the services this workload uses came to at most ${used:,.0f} of it." if used else "."))
    known = scope_context_text(scope_context)
    return text + ((" " + known) if known else "")


def glance_rows(workload, issues, checks, inventory, scope, scope_ids, scope_context, specialists=None):
    """The one-page summary as (label, html, markdown) rows, built only from what the report already shows further down:
    what was assessed (and what the account bill covers), what to confirm first, what is in good shape, the key issues in
    one line each with their declared size and list price, what can start this week and dated deadlines.
    Example prices for sizes still to be measured are not repeated here; the line says the size follows measurement."""
    rows = []
    text = scope_sentence(workload, inventory, scope, scope_ids, scope_context)
    rows.append(("Scope", kit.esc(text), text))
    facts = environment_facts(inventory, scope_ids, scope_context)
    if facts:
        text = (f"The scope interview names this production, but the oldest dated resource is {facts['age']} days old"
                + (" and tags name " + ", ".join(k for k, _ in facts["tags"]) if facts["tags"] else "")
                + ". The priorities below read the workload as production; if it has not launched, they are items to have in place before launch.")
        rows.append(("To confirm first", kit.esc(text), text))
    passed = [c for c in checks if c["status"] == "PASS" and c["rule_id"] not in ACCOUNT_RULES]
    if passed:
        sample = {}
        for c in passed:
            sample.setdefault(c["rule_id"], c)
        names = [rule_label(sample[r]) for r, _ in Counter(c["rule_id"] for c in passed).most_common(4)]
        rows.append(("Already in good shape", kit.esc(", ".join(names) + "."), ", ".join(names) + "."))
    if issues:
        def size(i):
            parts = [URGENCY_LABEL[i["urgency"]]]
            if i.get("effort") in EFFORT:
                parts.append(EFFORT[i["effort"]][0])
            net = net_list_price(i)
            if is_example(i):
                parts.append("size decided after measurement")
            elif net is not None:
                parts.append(f"List price {usd(net)}/month")
            elif i.get("cost_direction") in COST_DIRECTION:
                parts.append(COST_DIRECTION[i["cost_direction"]][0])
            return " · ".join(parts)
        html_items = "".join(f'<li><a href="#issue-{n}">{kit.esc(i["title"])}</a> <span class="small">({kit.esc(size(i))})</span></li>' for n, i in enumerate(issues, 1))
        md_items = " ".join(f"({n}) {i['title']} [{size(i)}]" for n, i in enumerate(issues, 1))
        rows.append(("Key issues", f'<ol class="glance-list">{html_items}</ol>', md_items))
        quick, _, firsts = board_groups(issues)
        starts = [(i.get("first_step") or {}).get("text") or i["title"] for _, i in quick] + [i["first_step"]["text"] for _, i in firsts]
        if starts:
            listed = "".join(f"<li>{kit.esc(s)}</li>" for s in starts[:4]) + (f'<li class="small">+{len(starts) - 4} more</li>' if len(starts) > 4 else "")
            rows.append(("Can start this week", f'<ul class="plain">{listed}</ul>', " ".join(f"({k}) {s}" for k, s in enumerate(starts, 1))))
    dated = dated_items(checks)
    if dated:
        text = "; ".join(f"{d} {label} ({target})" for d, label, target, _ in dated) + "."
        rows.append(("Has a date", kit.esc(text), text))
    if specialists:
        def first(sp):
            recs = sp.get("recommendations") or []
            return recs[0]["title"] if recs else "no change recommended for now"
        html_items = "".join(f'<li><a href="#sp-{kit.esc(sp["specialist"])}"><b>{kit.esc(sp["role"])}</b></a> · {kit.esc(first(sp))}'
                             + (f' <span class="small">(+{len(sp["recommendations"]) - 1} more)</span>' if len(sp.get("recommendations") or []) > 1 else "") + '</li>'
                             for sp in specialists)
        rows.append(("Specialist SA Agent views", f'<ul class="plain">{html_items}</ul>', " ".join(f"({sp['role']}) {first(sp)}" for sp in specialists)))
    return rows


def glance_html(rows):
    if not rows:
        return ""
    body = "".join(f'<tr><th>{kit.esc(label)}</th><td>{content}</td></tr>' for label, content, _ in rows)
    return (f'<section id="glance"><h2>At a glance</h2><p class="lead">One page for the first conversation. Every line is taken from the sections below; nothing here is new.</p>'
            f'<table class="glance">{body}</table></section>')


def glance_markdown(rows):
    if not rows:
        return []
    return ["## At a glance", ""] + [f"- **{label}** {text}" for label, _, text in rows] + [""]


# ---------------------------------------------------------------------------
# Report files (details.html is the details page of an earlier version, removed when found)
# ---------------------------------------------------------------------------
DETAILS_FILE = "details.html"
REPORT_FILE = "assessment.html"


def nav_html(items, other=None):
    """The sticky table of contents: same-page anchors, then a link to the other page of the pair."""
    links = "".join(f'<a href="#{kit.esc(a)}">{kit.esc(label)}</a>' for a, label in items)
    if other:
        href, label = other
        links += f'<a href="{kit.esc(href)}" class="other">{kit.esc(label)} →</a>'
    return f'<nav class="toc"><div class="wrap">{links}</div></nav>'


def ids_in(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


def cross_link(html, own_ids, other_file, other_ids):
    """Point same-page links whose anchor lives on the other page at that page. A link to an id neither page has is left
    alone (it is a defect the tests catch, not something to hide)."""
    def fix(m):
        target = m.group(1)
        if target in own_ids or target not in other_ids:
            return m.group(0)
        return f'href="{other_file}#{target}"'
    return re.sub(r'href="#([^"]+)"', fix, html)


def pillar_drivers_html(checks, screener, grades, open_groups):
    """Section 2's body: for each pillar, the failed and warned checks grouped by rule (one row per rule with the
    resources it applies to), with the open-question count. Passed checks and the full cards stay on the details page."""
    questions = Counter(members[0]["pillar"] for _, members in open_groups)
    out = []
    for pillar in PILLARS:
        key, info = grades[pillar]
        items = sorted([c for c in checks + screener if c["pillar"] == pillar and c["status"] in {"FAIL", "WARN"}],
                       key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
        passed = sum(1 for c in checks if c["pillar"] == pillar and c["status"] in {"PASS", "NOT_APPLICABLE"})
        rows = []
        for members in group_findings(items):
            first = members[0]
            targets = list(dict.fromkeys(target_of(m) for m in members))
            res = ", ".join(f"<code>{kit.esc(t)}</code>" for t in targets[:2]) + (f' <span class="small">+{len(targets) - 2} more</span>' if len(targets) > 2 else "")
            rows.append(f'<tr><td><a href="#{kit.esc(first["id"])}">{kit.esc(rule_label(first))}</a></td><td>{kit.status_chip(first["status"])}</td>'
                        f'<td>{kit.chip(kit.SEVERITY[first.get("severity", "INFO")][0], kit.SEVERITY[first.get("severity", "INFO")][1])}</td>'
                        f'<td>{len(members)}</td><td>{res}</td><td>{kit.esc(first["recommendation"])}</td></tr>')
        table = ('<table><thead><tr><th>Check</th><th>Status</th><th>Severity</th><th>Resources</th><th>Where</th><th>Recommendation</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table>') if rows else f'<p class="small">Nothing failed or warned in this pillar; {passed} check(s) passed.</p>'
        tail = (f'<p class="small">{questions[pillar]} open question(s) the configuration cannot answer are listed <a href="#{anchor(pillar)}">with this pillar\'s findings</a>.</p>'
                if questions.get(pillar) else "")
        out.append(f'<details id="drivers-{anchor(pillar)}"{" open" if key in {"critical", "weak"} else ""}><summary>{kit.esc(pillar)} {kit.grade_chip(key)}'
                   f'<span class="small">{kit.esc(kit.grade_counts_text(info))}</span></summary><div class="body"><p class="small">{kit.esc(kit.GRADES[key][3])}</p>{table}{tail}</div></details>')
    return "".join(out)


def pillar_drivers_markdown(checks, screener, grades):
    lines = []
    for pillar in grades:
        key, info = grades[pillar]
        items = sorted([c for c in checks + screener if c["pillar"] == pillar and c["status"] in {"FAIL", "WARN"}],
                       key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
        lines += [f"### {pillar} - {kit.GRADES[key][0]} ({kit.grade_counts_text(info)})", ""]
        groups = group_findings(items)
        if not groups:
            lines += ["Nothing failed or warned in this pillar.", ""]
            continue
        lines += ["| Check | Status | Severity | Resources | Recommendation |", "|---|---|---|---|---|"]
        for members in groups:
            first = members[0]
            targets = list(dict.fromkeys(target_of(m) for m in members))
            lines.append(f"| {rule_label(first)} `{first['id']}` | {first['status']} | {first.get('severity', '')} | {len(members)}: {', '.join(targets[:2])}{' +' + str(len(targets) - 2) if len(targets) > 2 else ''} | {first['recommendation'].replace('|', '/')} |")
        lines.append("")
    return lines


def rec_current(rec, specialist, by_id):
    """The first of a recommendation's four fields: what is there now. The specialist's own `current` sentence when it
    wrote one; otherwise the observations of the findings it cites; otherwise its current-state items that share a
    reference. Observations are evidence text, not model text."""
    if str(rec.get("current") or "").strip():
        return rec["current"]
    seen = []
    for ref in rec.get("refs") or []:
        c = by_id.get(ref)
        if c and c.get("observation") and c["observation"] not in seen:
            seen.append(c["observation"])
    if seen:
        return " ".join(seen[:2])
    refs = set(rec.get("refs") or [])
    shared = [i["text"] for i in specialist.get("current_state") or [] if refs & set(i.get("refs") or [])]
    return " ".join(shared[:2]) if shared else "See what the specialist sees now, above."


def rec_evidence_rows(rec):
    """(metric, value, source, kind) rows for the third field. The figures benefits.py computed from the evidence
    (`evidence`) first, then the list prices the specialist cited, then the one figure the owner provides; the renderer
    adds nothing of its own."""
    computed = [e for e in rec.get("evidence") or [] if isinstance(e, dict)]
    rows = [(str(e.get("metric", "")), str(e.get("value", "")), str(e.get("source", "")), e.get("kind", "")) for e in computed if e.get("kind") != "input_needed"]
    prices = rec.get("list_prices") or []
    for p in prices:
        rows.append((f"List price {p['sign']} {p['label']}", f"${p['monthly_usd']:,.0f}/month", f"{p['basis']}; AWS Price List API, On-Demand ({p['id']})", "price"))
    if len(prices) > 1:
        rows.append(("Net list price", f"{usd(net_list_price(rec))}/month", "sum of the list prices above; not a bill forecast", "price"))
    rows += [(str(e.get("metric", "")), str(e.get("value", "")), str(e.get("source", "")), "input_needed") for e in computed if e.get("kind") == "input_needed"]
    return rows


def rec_fields_html(n, rec, specialist, by_id, finding_ids):
    """One recommendation as the fixed four fields: current state, proposal, quantitative evidence, prerequisites."""
    evidence = rec_evidence_rows(rec)
    if evidence:
        ev = ('<table class="inner"><thead><tr><th>Metric</th><th>Value</th><th>Source</th></tr></thead><tbody>'
              + "".join(f'<tr{" class=\"input-needed\"" if k == "input_needed" else ""}><td>{kit.esc(m)}</td><td>{kit.esc(v)}</td><td class="small">{kit.esc(s)}</td></tr>' for m, v, s, k in evidence) + '</tbody></table>')
        if is_example(rec):
            ev += '<p class="small">An example at published On-Demand rates for one possible configuration; the size is decided after measurement, so the figure is not a saving to plan on.</p>'
    else:
        ev = '<p class="small">No figure was computed from the evidence for this change; the counts and dates in the findings it cites are the numbers available.</p>'
    pre = (f'<p><b>Before deciding</b> {kit.esc(join_sentences(rec.get("before_deciding")))}</p>' if rec.get("before_deciding") else "")
    pre += (f'<p class="small"><b>Alternatives</b> {kit.esc(join_sentences(rec.get("alternatives")))}</p>'
            f'<p class="small"><b>How to undo</b> {kit.esc(rec["rollback"])}</p>')
    return (f'<div class="rec"><h4>{n}. {kit.esc(rec["title"])} <span class="chips">{issue_chips(rec)}</span></h4><table class="fields">'
            f'<tr><th>Current state</th><td>{kit.esc(rec_current(rec, specialist, by_id))}</td></tr>'
            f'<tr><th>Proposal</th><td><p><b>What changes</b> {kit.esc(rec["change"])}</p><p><b>What it gives</b> {kit.esc(rec["benefit"])}</p></td></tr>'
            f'<tr><th>Quantitative evidence</th><td>{ev}</td></tr>'
            f'<tr><th>Prerequisites</th><td>{pre}</td></tr></table>'
            f'<div class="ref">Based on {ref_links(rec.get("refs"), finding_ids)}</div></div>')


def rec_fields_markdown(n, rec, specialist, by_id):
    lines = [f"**{n}. {rec['title']}** ({EFFORT[rec['effort']][0]} · {COST_DIRECTION[rec['cost_direction']][0]})", "",
             f"- Current state: {rec_current(rec, specialist, by_id)}", f"- Proposal: {rec['change']} What it gives: {rec['benefit']}"]
    evidence = rec_evidence_rows(rec)
    if evidence:
        lines += ["- Quantitative evidence:", "", "  | Metric | Value | Source |", "  |---|---|---|"]
        lines += [f"  | {m.replace('|', '/')} | {v} | {s.replace('|', '/')} |" for m, v, s, _ in evidence]
        if is_example(rec):
            lines.append("  An example before measurement, not a saving to plan on.")
        lines.append("")
    else:
        lines.append("- Quantitative evidence: no figure computed from the evidence for this change.")
    if rec.get("before_deciding"):
        lines.append(f"- Prerequisites: {join_sentences(rec['before_deciding'])}")
    lines += [f"- Alternatives: {join_sentences(rec.get('alternatives'))}", f"- How to undo: {rec['rollback']}", f"- Based on `{', '.join(rec.get('refs') or [])}`", ""]
    return lines


# ---------------------------------------------------------------------------
# The paged report: Executive summary, AS-IS architecture, Well-Architected pillars, Modernization, Appendix
# ---------------------------------------------------------------------------
PAGES = [("p-summary", "Executive summary"), ("p-asis", "AS-IS architecture"), ("p-pillars", "Well-Architected pillars"),
         ("p-modern", "Modernization recommendations"), ("p-appendix", "Appendix")]
SPECIALIST_MARK = {"containers": ("C", "#ED7100"), "database": ("D", "#C925D1"), "analytics": ("A", "#8C4FFF"), "genai": ("G", "#01A88D"), "devops": ("O", "#E7157B")}
MATRIX_COLUMNS = [("setting", "Setting change"), ("days", "Days of work"), ("weeks", "Weeks of work"), ("quarter", "Architecture change")]
MATRIX_ROWS = [("down", "Cost down"), ("neutral", "Cost neutral"), ("measure_first", "Measure first"), ("up", "Cost up")]


def issue_pillar(issue, by_id):
    """The pillar a key issue belongs to: the pillar most of its findings sit in (the first one on a tie)."""
    pillars = [by_id[f]["pillar"] for f in issue.get("finding_ids") or [] if f in by_id]
    if not pillars:
        return None
    counts = Counter(pillars)
    return max(pillars, key=lambda p: (counts[p], -pillars.index(p)))


def first_sentence(text, limit=220):
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def issue_cards(numbered):
    """Key-issue cards for (number, issue) pairs; the number is the issue's place in the whole list."""
    out = []
    for n, issue in numbered:
        label, color = URGENCY[issue["urgency"]]
        refs = " ".join(f'<a href="#{kit.esc(r)}">{kit.esc(r)}</a>' for r in issue["finding_ids"])
        links = "".join(f'<li><a href="{kit.esc(r["url"])}" target="_blank" rel="noopener">{kit.esc(r["title"])}</a></li>' for r in issue.get("resolved_references", []))
        out.append(f'<article class="issue" id="issue-{n}" style="border-left-color:{color}"><h3>{n}. {kit.esc(issue["title"])} <span class="chips">{kit.chip(label, color)}{issue_chips(issue)}</span></h3>'
                   f'<p>{kit.esc(issue["problem"])}</p><p class="why"><b>Why it matters</b> {kit.esc(issue["impact"])}</p>'
                   f'<p class="act"><b>Suggestion</b> {kit.esc(issue["action"])}</p>{price_lines(issue)}'
                   + (f'<div class="small"><b>References</b><ul>{links}</ul></div>' if links else "") +
                   f'<div class="ref">Findings {refs}</div></article>')
    return "".join(out)


def improvements_html(issues, by_id):
    """The key improvements of the executive summary: each key issue in one line, its size, and where it is detailed."""
    if not issues:
        return ('<div class="callout"><p><b>Key issues were not authored for this run.</b> The grades and findings are complete, but the plain-language '
                'summary requires the assessing agent to supply key-issues.json.</p></div>')
    items = []
    for n, i in enumerate(issues, 1):
        label, color = URGENCY[i["urgency"]]
        pillar = issue_pillar(i, by_id)
        items.append(f'<li style="border-left-color:{color}"><b>{kit.esc(i["title"])}</b><span class="chips">{kit.chip(label, color)}{issue_chips(i)}</span>'
                     f'<p class="small">{kit.esc(first_sentence(i["action"]))} <a href="#issue-{n}">{kit.esc(pillar or "Details")} →</a></p></li>')
    return f'<ol class="improvements">{"".join(items)}</ol>'


def matrix_points(issues, specialists):
    points, legend_items = [], []
    for n, i in enumerate(issues, 1):
        if i.get("effort") in EFFORT and i.get("cost_direction") in COST_DIRECTION:
            points.append((str(n), i["effort"], i["cost_direction"], kit.ACCENT, f"#issue-{n}"))
    if points:
        legend_items.append(("Key issue (number)", kit.ACCENT))
    for sp in specialists or []:
        mark, color = SPECIALIST_MARK.get(sp["specialist"], (sp["specialist"][:1].upper(), kit.MUTED))
        recs = sp.get("recommendations") or []
        for n, r in enumerate(recs, 1):
            points.append((f"{mark}{n}", r.get("effort"), r.get("cost_direction"), color, f"#sp-{sp['specialist']}"))
        if recs:
            legend_items.append((f"{mark} = {sp['role']}", color))
    return points, legend_items


def matrix_html(issues, specialists):
    points, legend_items = matrix_points(issues, specialists)
    if not points:
        return ""
    return (f'<div class="chart-card"><h3>Where each change sits</h3><p class="small">Columns are the size of the work, rows the cost direction. '
            f'The top-left cells (small and not raising cost) are where to start. Click a dot to open its detail.</p>'
            f'{kit.priority_matrix(points, MATRIX_COLUMNS, MATRIX_ROWS, legend_items=legend_items)}</div>')


def radar_html(scores, grades):
    values = {p: (scores.get(p) or {}).get("score") for p in PILLARS}
    colors = {p: kit.GRADES[grades[p][0]][1] for p in PILLARS}
    rows = "".join(f'<tr><td><a href="#wa-{anchor(p)}"><b>{kit.esc(p)}</b></a></td><td>{kit.grade_chip(grades[p][0])}</td>'
                   f'<td>{kit.segment_bar((scores.get(p) or {}).get("counts") or {})}</td><td class="small">{kit.esc(kit.grade_counts_text(grades[p][1]))}</td></tr>' for p in PILLARS)
    return (f'<div class="grid g2"><div class="chart-card"><h3>Six pillars at a glance</h3><p class="small">Local score 0-100 per pillar (PASS 1, WARN 0.5, FAIL 0 over the judged checks); '
            f'the point colour is the grade. An index for comparison, not an AWS Well-Architected Tool rating.</p>{kit.radar(values, colors=colors)}</div>'
            f'<div class="chart-card"><h3>Grades</h3><table class="grade-list"><tbody>{rows}</tbody></table>'
            f'{kit.legend([(kit.STATUS[k][0], kit.STATUS[k][1]) for k in kit.STATUS_ORDER])}</div></div>')


def resource_mix_html(inventory, scope_ids):
    resources = [r for r in inventory.get("resources", []) if r.get("level") != "account" and (scope_ids is None or r["id"] in scope_ids)]
    if not resources:
        return ""
    counts = Counter(r["type"].split(".")[0].upper() if r["type"].split(".")[0] not in SERVICE_WORDS else SERVICE_WORDS[r["type"].split(".")[0]] for r in resources)
    pairs = counts.most_common(8)
    other = sum(counts.values()) - sum(v for _, v in pairs)
    if other:
        pairs.append(("Other", other))
    return (f'<div class="chart-card"><h3>What the workload is made of</h3><p class="small">{len(resources)} resources in scope, by service.</p>'
            f'{kit.count_bars(pairs, "resource-mix", "Resources by service", "Horizontal bars count the in-scope resources of each service.", focal=pairs[0][0] if pairs else None)}</div>')


SERVICE_WORDS = {"ec2": "EC2 and VPC", "elbv2": "Load balancing", "ecs": "ECS", "eks": "EKS", "rds": "RDS and Aurora", "elasticache": "ElastiCache", "logs": "CloudWatch Logs",
                 "cloudwatch": "CloudWatch", "s3": "S3", "lambda": "Lambda", "iam": "IAM", "secretsmanager": "Secrets Manager", "ecr": "ECR", "sns": "SNS", "sqs": "SQS",
                 "events": "EventBridge", "codepipeline": "CodePipeline", "codebuild": "CodeBuild", "codecommit": "CodeCommit", "kms": "KMS", "dynamodb": "DynamoDB",
                 "bedrock": "Bedrock", "autoscaling": "Auto Scaling", "cloudformation": "CloudFormation", "ssm": "Systems Manager", "acm": "ACM", "wafv2": "WAF"}


def pillar_driver_table(pillar, checks, screener):
    items = sorted([c for c in checks + screener if c["pillar"] == pillar and c["status"] in {"FAIL", "WARN"}],
                   key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
    rows = []
    for members in group_findings(items):
        first = members[0]
        targets = list(dict.fromkeys(target_of(m) for m in members))
        res = ", ".join(f"<code>{kit.esc(t)}</code>" for t in targets[:2]) + (f' <span class="small">+{len(targets) - 2} more</span>' if len(targets) > 2 else "")
        sev = first.get("severity", "INFO")
        rows.append(f'<tr><td><a href="#{kit.esc(first["id"])}">{kit.esc(rule_label(first))}</a></td><td>{kit.status_chip(first["status"])}</td>'
                    f'<td>{kit.chip(kit.SEVERITY[sev][0], kit.SEVERITY[sev][1])}</td><td>{len(members)}</td><td>{res}</td><td>{kit.esc(first["recommendation"])}</td></tr>')
    if not rows:
        return ""
    return ('<table><thead><tr><th>Check</th><th>Status</th><th>Severity</th><th>Resources</th><th>Where</th><th>Recommendation</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def pillar_blocks_html(checks, screener, grades, scores, issues, by_id, open_groups):
    """One block per pillar: grade and counts, the key issues that belong to it, the checks that drive the grade and
    what is in good shape. Security is a pillar like the others; there is no separate security chapter."""
    by_pillar = defaultdict(list)
    for n, i in enumerate(issues, 1):
        by_pillar[issue_pillar(i, by_id)].append((n, i))
    questions = Counter(members[0]["pillar"] for _, members in open_groups)
    out = []
    for pillar in PILLARS:
        key, info = grades[pillar]
        mine = by_pillar.get(pillar, [])
        table = pillar_driver_table(pillar, checks, screener)
        passed = [c for c in checks if c["pillar"] == pillar and c["status"] == "PASS"]
        good = Counter(rule_label(c) for c in passed).most_common(4)
        body = f'<p class="small">{kit.esc(kit.GRADES[key][3])}</p>'
        if mine:
            body += f'<h4>Key issues in this pillar ({len(mine)})</h4>' + issue_cards(mine)
        if table:
            body += f'<h4>Checks that drive the grade</h4>{table}'
        elif not mine:
            body += f'<p>Nothing failed or warned in this pillar; {len(passed)} check(s) passed.</p>'
        tail = []
        if good:
            tail.append("<b>In good shape</b> " + kit.esc(", ".join(f"{name} ({n})" for name, n in good)))
        if questions.get(pillar):
            tail.append(f'<b>Open questions</b> {questions[pillar]}, listed with <a href="#{anchor(pillar)}">this pillar\'s findings</a> in the appendix')
        if tail:
            body += '<p class="small">' + " · ".join(tail) + "</p>"
        out.append(f'<article class="pillar" id="wa-{anchor(pillar)}" style="border-top:4px solid {kit.GRADES[key][1]}"><header><h3>{kit.esc(pillar)}</h3>{kit.grade_chip(key)}'
                   f'{kit.segment_bar((scores.get(pillar) or {}).get("counts") or {})}<span class="small">{kit.esc(kit.grade_counts_text(info))}</span></header>{body}</article>')
    return "".join(out)


def specialist_overview_html(specialists, statuses):
    if not specialists:
        return ""
    rows = []
    for sp in specialists:
        mark, color = SPECIALIST_MARK.get(sp["specialist"], ("?", kit.MUTED))
        recs = sp.get("recommendations") or []
        sizes = Counter(EFFORT.get(r.get("effort"), ("?",))[0] for r in recs)
        chips = "".join(kit.pathway_chip(statuses[p]) for p in sp.get("pathways", []) if p in statuses)
        first = kit.esc(recs[0]["title"]) if recs else '<span class="small">no change recommended for now</span>'
        rows.append(f'<tr><td><span class="chip" style="background:{color};color:#fff">{mark}</span> <a href="#sp-{kit.esc(sp["specialist"])}"><b>{kit.esc(sp["role"])}</b></a></td>'
                    f'<td>{chips}</td><td>{len(recs)}</td><td class="small">{kit.esc(", ".join(f"{k} {v}" for k, v in sizes.items()) or "-")}</td><td>{first}</td></tr>')
    return ('<table><thead><tr><th>Specialist SA Agent</th><th>Directions</th><th>Recommendations</th><th>Size</th><th>First recommendation</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def render(args):
    run = args.run_dir
    WALL_CLOCK["minutes"] = args.wall_clock_minutes
    WALL_CLOCK["end"] = last_stage_write(run)
    findings = read_json(run / "04-review/findings.json"); scores = read_json(run / "04-review/scorecard.json")
    as_is = resolve_as_is(run, findings)
    inventory = read_json(as_is / "inventory.json"); manifest = read_json(as_is / "collection-manifest.json")
    pathways_doc = read_json(run / "05-modernization/pathways.json")
    scope_doc = read_json(run / "scope.json") if (run / "scope.json").is_file() else {}
    scope_context = scope_doc.get("context") or {}
    scope_ids = {r["id"] for r in read_json(as_is / "scope-resources.json").get("resources", [])} if (as_is / "scope-resources.json").is_file() else None
    for key in ("account_id", "run_id"):
        if not (inventory.get(key) == findings.get(key) == pathways_doc.get(key)):
            raise ValueError(f"input {key} mismatch between stages")
    checks = findings["checks"]; pathways = pathways_doc["candidates"]
    KNOWN_USERS.clear()
    KNOWN_USERS.update(iam_user_names(inventory, findings))
    BENEFITS["result"] = None
    finding_ids = {c["id"] for c in checks}
    screener, upstream_manifest = [], None
    if args.screener:
        sdoc = read_json(args.screener)
        if sdoc.get("account_id") != inventory["account_id"]:
            raise ValueError("imported findings belong to another account")
        screener = [c for c in sdoc["checks"] if c.get("status") in {"FAIL", "WARN"}]
        finding_ids |= {c["id"] for c in screener}
        um = args.screener.parent / "upstream-manifest.json"
        upstream_manifest = read_json(um) if um.is_file() else {"input_sha256": sdoc.get("input_sha256", ""), "account_id": sdoc.get("account_id"), "regions": [], "findings": len(screener)}
    prices = load_list_prices(run)
    issues = load_key_issues(args.key_issues, finding_ids, prices)
    specialists = load_specialists(run, checks, screener, manifest, inventory, prices)
    statuses = {c["pathway"]: c["status"] for c in pathways}
    by_id = {c["id"]: c for c in checks + screener}
    for issue in issues:
        issue["resolved_references"] = issue_references(issue, by_id)
    changes = iac_changes(checks)
    audit_path = args.diagram_audit or run / "04-review/diagrams/diagram-audit.json"
    diagram_audit = read_json(audit_path) if Path(audit_path).is_file() else None
    diagram_ok = bool(diagram_audit) and diagram_audit.get("status") == "pass" and (diagram_audit.get("coverage") or {}).get("status") == "pass"
    run_meta = kit.load_run_metadata(args.run_metadata)
    write_audit = read_json(run / "write-audit.json") if (run / "write-audit.json").is_file() else None
    grades = kit.grade_all(checks, PILLARS)
    as_is_svg = safe_svg(args.as_is_diagram) if args.as_is_diagram else None
    to_be_svg = safe_svg(args.to_be_diagram) if args.to_be_diagram else None
    status_counts = Counter(c["status"] for c in checks)
    worst = [p for p in PILLARS if grades[p][0] == "critical"] or [p for p in PILLARS if grades[p][0] == "weak"]
    recommended = [c for c in pathways if c["status"] in {"candidate", "conditional"}]
    draft = not (as_is_svg and (to_be_svg or not recommended)) or not diagram_ok
    status = "Final" if not draft else ("Draft - diagram does not cover every collected resource" if as_is_svg and not diagram_ok else "Draft - diagrams pending")
    anchors = {p: anchor(p) for p in PILLARS}

    hero = kit.hero("AWS modernization assessment", f"{args.workload}: current state and modernization directions",
                    f"An executive summary with the key improvements, the AS-IS architecture, recommendations for each of the six Well-Architected pillars "
                    f"({len(checks) + len(screener)} automated checks; not an AWS Well-Architected Tool review) and the modernization recommendations of the "
                    f"Specialist SA Agents. Produced with AI assistance from read-only evidence; worth confirming with the assessing architect before decisions are "
                    f"made. Evidence, method and provenance are in the appendix.",
                    [f"account {inventory['account_id']}", ", ".join(inventory["regions"]), f"collected {str(manifest.get('completed_at', ''))[:16]}", status]
                    + ([f"environment: {scope_context['environment']} (scope interview)"] if scope_context.get("environment", "unknown") != "unknown" else []))
    status_counts.update(c["status"] for c in screener)
    everything = checks + screener
    open_groups = question_groups(checks, scope_context)

    def listing(status):
        return [(f"#{c['id']}", f"{c['id']} · {rule_label(c)} · {target_of(c)}") for c in sorted((x for x in everything if x["status"] == status),
                key=lambda c: (SEVERITY_ORDER.get(c.get("severity"), 9), c["id"]))]
    kpis = (kit.kpi(len(issues) or "-", "key issues", "fail" if issues else "unknown", [(f"#issue-{n}", i["title"]) for n, i in enumerate(issues, 1)])
            + kit.kpi(status_counts["FAIL"], "failed checks", "fail", listing("FAIL"))
            + kit.kpi(status_counts["WARN"], "warnings", "warn", listing("WARN"))
            + kit.kpi(len(open_groups), "open questions", "unknown", [(f"#q-{members[0]['id']}", f"{members[0]['id']} · {text}") for text, members in open_groups])
            + kit.kpi(len(recommended), "recommended directions", "accent", [("#directions", f"{PATHWAYS[c['pathway']]} · {kit.PATHWAY_STATUS[c['status']][0]}") for c in recommended]))
    evidence_rows = evidence_index(manifest, checks, screener, specialists)
    worst_text = (f"Most urgent pillars: {', '.join(worst)}." if worst else "No pillar is graded critical or weak.")
    to_be_block = (zoom_figure("zoom-to-be", to_be_svg, "directions", f'Proposed target architecture · retained, proposed and unresolved parts are distinguished in the legend · click the diagram to enlarge · {open_full(args.to_be_diagram, run)}', args.to_be_diagram) if to_be_svg
                   else '<div class="callout"><p>The target-architecture diagram has not been produced yet.</p></div>') + to_be_changes_html(args.to_be_diagram)
    as_is_block = (zoom_figure("zoom-as-is", as_is_svg, "p-asis", f'Current architecture from observed configuration · arrows are configured paths, not measured traffic · click the diagram to enlarge · {open_full(args.as_is_diagram, run)}', args.as_is_diagram) if as_is_svg
                   else '<div class="callout"><p>The current-architecture diagram has not been produced yet; this report is a draft until it is.</p></div>')
    if as_is_svg and not diagram_ok:
        cov = (diagram_audit or {}).get("coverage") or {}
        missing = ", ".join(f"{k} × {v}" for k, v in (cov.get("missing_by_type") or {}).items()) or "audit not run"
        as_is_block += (f'<div class="callout"><p><b>The diagram does not yet show everything that was collected.</b> Missing: {kit.esc(missing)}'
                        f'{"; subnets/AZs not drawn as groups" if cov.get("subnets_or_vpcs_not_drawn_as_groups") or cov.get("missing_availability_zones") else ""}'
                        f'{"; stack resources " + str(len(cov.get("missing_stack_resources", []))) if cov.get("missing_stack_resources") else ""}. '
                        'Run diagram.py with --inventory and fix the graph before delivery.</p></div>')
    collection = (f'<p class="small">Evidence: {manifest["coverage"].get("ok", 0)} successful read-only queries, collection status <b>{kit.esc(manifest["status"])}</b>, '
                  f'{len(inventory["resources"])} resources in {", ".join(inventory["regions"])}. Host internals, database contents, traffic and cost were not collected. '
                  f'Account-level services and the query scope are in <a href="#account">the appendix</a>.</p>')
    scope_text = scope_sentence(args.workload, inventory, scope_doc, scope_ids, scope_context)
    headline = []
    for sp in specialists:
        recs = sp.get("recommendations") or []
        headline.append(f'<li><a href="#sp-{kit.esc(sp["specialist"])}"><b>{kit.esc(sp["role"])}</b></a> · {kit.esc(recs[0]["title"]) if recs else "no change recommended for now"}</li>')
    pages = {}
    pages["p-summary"] = (
        f'<h2>1 · Executive summary</h2><p class="lead">{kit.esc(scope_text)}</p>{environment_note(inventory, scope_ids, scope_context)}'
        f'<div class="kpis">{kpis}</div>{radar_html(scores, grades)}<p class="small">{kit.esc(worst_text)} Counts grow with the number of resources checked; they are an index, not a verdict.</p>'
        f'<h3>Key improvements</h3><p class="lead">The changes worth deciding on first, in order. Each links to its full description under its pillar.</p>{improvements_html(issues, by_id)}'
        f'{live_signals_html(checks)}<h3>Where to start</h3>{quick_wins_html(issues, checks)}{matrix_html(issues, specialists)}'
        + (f'<h3>Modernization in one line per area</h3><ul class="plain">{"".join(headline)}</ul>' if headline else ""))
    pages["p-asis"] = (
        f'<h2>2 · AS-IS architecture</h2><p class="lead">{kit.esc(scope_text)}</p>{as_is_block}{collection}{resource_mix_html(inventory, scope_ids)}')
    pages["p-pillars"] = (
        f'<h2>3 · Recommendations by Well-Architected pillar</h2><p class="lead">{kit.esc(worst_text)} Each pillar shows its grade, the key issues that belong to it '
        f'with the suggestion, and the failed and warned checks that drive the grade. Fix first = a high-severity item; Needs work = failures or several warnings; '
        f'Watch = minor warnings or open questions; Sound = every judged check passed.</p>'
        f'{kit.grade_tiles(grades, {p: "wa-" + anchors[p] for p in PILLARS}, rule_labels=rule_label)}'
        f'<div class="chart-card"><h3>Judgements per pillar</h3>{kit.stacked_status(scores)}{kit.legend([(kit.STATUS[k][0], kit.STATUS[k][1]) for k in kit.STATUS_ORDER])}</div>'
        f'{pillar_blocks_html(checks, screener, grades, scores, issues, by_id, open_groups)}'
        f'<p class="small">Priority words on each issue: <b>1 · Decide first</b> = settle in the first conversation; <b>2 · Plan next</b> = the next piece of planned work; '
        f'<b>3 · Later</b> = after that. A suggestion, not a deadline.</p>')
    if specialists:
        pages["p-modern"] = (
            f'<h2>4 · Modernization recommendations</h2><p class="lead">{len(specialists)} Specialist SA Agents, AI agents that each take the role of an AWS specialist '
            f'solutions architect, reviewed the evidence in their area. Every recommendation has the same four fields: current state, proposal, quantitative evidence '
            f'(counts, dates, pipeline history and the account bill computed from the evidence, list prices from the AWS Price List API, and the one figure only the owner '
            f'can provide) and prerequisites.</p>{specialist_overview_html(specialists, statuses)}<div id="directions"></div>'
            + "".join(specialist_html(sp, statuses, finding_ids, run, by_id) for sp in specialists)
            + f'<h3>Combined target architecture</h3>{to_be_block}<h3>Seven directions</h3>{pathway_table(pathways, specialists)}')
    else:
        pages["p-modern"] = (f'<h2>4 · Modernization directions</h2><p class="lead">Seven directions were compared against the evidence. Each card states '
                             f'whether it is recommended, under what conditions, or why not.</p><div id="directions"></div>{pathway_cards(pathways)}<h3>Proposed target architecture</h3>{to_be_block}')
    pages["p-appendix"] = (
        f'<h2>Appendix</h2><p class="lead">What the four sections rest on. Links from the sections above open the matching part here.</p>'
        f'<h3 id="evidence"><span class="appendix-letter">A</span>Evidence</h3>{evidence_html(evidence_rows)}'
        f'<h3 id="appendix"><span class="appendix-letter">B</span>Method and provenance</h3>{method_appendix(scores, checks, pathways, pathways_doc["weights"])}{run_clock_html(inventory)}{write_audit_html(write_audit)}{usage_html(run_meta, inventory)}'
        f'<details><summary>Scope and limitations</summary><div class="body"><ul><li>Services in scope: {kit.esc(", ".join(inventory["services"]))}.</li><li>Query results: <code>{kit.esc(json.dumps(manifest["coverage"]))}</code>.</li>'
        f'<li>Configuration evidence does not establish traffic, application compatibility, recovery success, cost or business criticality; those need separate evidence.</li></ul></div></details>'
        f'<h3 id="findings"><span class="appendix-letter">C</span>All findings by pillar</h3><p class="small">Failures, warnings and open questions first; passed checks are folded away. {kit.esc(CODES_SENTENCE)}</p>{pillar_sections(checks, grades, screener, scope_context)}'
        f'<h3 id="account"><span class="appendix-letter">D</span>Account-level services and query scope</h3>{account_posture_html(checks, manifest, inventory, scope_context, scope_ids)}'
        f'<h3 id="next"><span class="appendix-letter">E</span>Reference material</h3>{next_steps_html(pathways_doc.get("next_steps") or {}, args.language)}'
        f'<h3 id="terms"><span class="appendix-letter">F</span>Terms used in this report</h3>@@TERMS@@')
    generated = datetime.now(timezone.utc).isoformat(timespec="minutes")
    sections = "".join(f'<section class="page" id="{pid}">{pages[pid]}{kit.pager(PAGES, k)}</section>' for k, (pid, _) in enumerate(PAGES))
    body = (f'{hero}{kit.pages_nav(PAGES)}<div class="wrap">{sections}'
            f'<footer>findings.json · scorecard.json · pathways.json · diagrams · {kit.esc(status)} · generated {generated}</footer></div>')
    body = body.replace("@@TERMS@@", terms_html(re.sub(r"<[^>]+>", " ", body)))
    html_doc = kit.page(f"AWS modernization assessment - {args.workload}", body, extra_css=kit.pages_css([pid for pid, _ in PAGES]))

    # Markdown twin, in the same order.
    head = [f"- Account `{inventory['account_id']}` · {', '.join(inventory['regions'])} · collected {str(manifest.get('completed_at', ''))[:16]} · {status}",
            f"- {len(checks) + len(screener)} checks · {status_counts['FAIL']} failed · {status_counts['WARN']} warnings · {len(open_groups)} open questions", ""]
    md = [f"# AWS modernization assessment - {args.workload}", ""] + head
    md += ["## 1. Executive summary", "", scope_text, "", worst_text, "", "| Pillar | Grade | Score | Counts |", "|---|---|---:|---|"]
    for p_ in PILLARS:
        k, info = grades[p_]
        sc = (scores.get(p_) or {}).get("score")
        md.append(f"| {p_} | {kit.GRADES[k][0]} | {sc if sc is not None else '-'} | {kit.grade_counts_text(info)} |")
    md += ["", "### Key improvements", ""]
    if issues:
        for n, i in enumerate(issues, 1):
            tags = [URGENCY_LABEL[i["urgency"]]] + [EFFORT[i["effort"]][0] for _ in [0] if i.get("effort") in EFFORT] + \
                   [COST_DIRECTION[i["cost_direction"]][0] for _ in [0] if i.get("cost_direction") in COST_DIRECTION]
            md.append(f"{n}. **{i['title']}** ({' · '.join(tags)}) {first_sentence(i['action'])}")
    else:
        md.append("Key issues were not authored for this run (key-issues.json missing).")
    if specialists:
        md += ["", "### Modernization in one line per area", ""] + [f"- **{sp['role']}** {(sp.get('recommendations') or [{'title': 'no change recommended for now'}])[0]['title']}" for sp in specialists]
    md += ["", "## 2. AS-IS architecture", "", scope_text, "",
           f"Evidence: {manifest['coverage'].get('ok', 0)} successful read-only queries, collection status {manifest['status']}, {len(inventory['resources'])} resources. "
           "Host internals, database contents, traffic and cost were not collected.", ""]
    md += ["## 3. Recommendations by Well-Architected pillar", ""]
    by_pillar = defaultdict(list)
    for n, i in enumerate(issues, 1):
        by_pillar[issue_pillar(i, by_id)].append((n, i))
    for p_ in PILLARS:
        k, info = grades[p_]
        md += [f"### {p_} - {kit.GRADES[k][0]} ({kit.grade_counts_text(info)})", ""]
        for n, i in by_pillar.get(p_, []):
            md += [f"#### {n}. {i['title']} ({URGENCY_LABEL[i['urgency']]})", "", i["problem"], "", f"**Why it matters** {i['impact']}", "", f"**Suggestion** {i['action']}", ""]
            if i.get("list_prices"):
                md += [f"**List prices** net {usd(net_list_price(i))}/month " + ("(an example before measurement)" if is_example(i) else "(AWS Price List API, On-Demand; not a bill forecast)"), ""]
                md += price_table(i["list_prices"])
            if i["resolved_references"]:
                md += ["**References**", ""] + [f"- [{r['title']}]({r['url']})" for r in i["resolved_references"]] + [""]
            md += [f"Findings: `{', '.join(i['finding_ids'])}`", ""]
        rows = pillar_drivers_markdown(checks, screener, {p_: grades[p_]})[2:]
        md += rows
    if specialists:
        md += ["## 4. Modernization recommendations", ""]
        for sp in specialists:
            md += specialist_markdown(sp, statuses, by_id)
        md += ["### Seven directions", ""]
    else:
        md += ["## 4. Modernization directions", ""]
    md += ["| Direction | Status | Reason |", "|---|---|---|"]
    for c in pathways:
        md.append(f"| {PATHWAYS[c['pathway']]} | {kit.PATHWAY_STATUS[c['status']][0]} | {(c['rationale'] if c['status'] in {'candidate', 'conditional'} else c['problem']).replace('|', '/')} |")
    md += ["", "## Appendix", "", "### A. Evidence", "", CODES_SENTENCE + " Raw responses stay in the run directory.", "",
           "| ID | Call | Target / locator | Kind | Collected (UTC) | Used by |", "|---|---|---|---|---|---|"]
    md += [f"| {r['id']} | `{r['what']}` | {r['target']} | {r['kind']} | {r['when']} | {', '.join(r['findings'][:6])}{' +' + str(len(r['findings']) - 6) if len(r['findings']) > 6 else ''} |" for r in evidence_rows]
    md += ["", "### B. Method and provenance", ""] + usage_markdown(run_meta, inventory) + write_audit_markdown(write_audit)
    md += ["### C. All findings by pillar", ""]
    for p_ in PILLARS:
        items = sorted([c for c in checks + screener if c["pillar"] == p_ and c["status"] in {"FAIL", "WARN"}], key=lambda c: (STATUS_ORDER[c["status"]], SEVERITY_ORDER.get(c.get("severity"), 9)))
        md += [f"#### {p_}", ""]
        for members in group_findings(items):
            c = members[0]
            label = CONFIDENCE_LABEL.get(c.get("confidence", "unverified"), c.get("confidence"))
            if len(members) == 1:
                md.append(f"- **{c['id']} · {rule_label(c)} · {c['status']} · {c['severity']}** ({label}) `{target_of(c)}` - {c['observation']} → {c['recommendation']}")
            else:
                md.append(f"- **{rule_label(c)} · {c['status']} · {c['severity']} · {len(members)} resources** ({label}) → {c['recommendation']}")
                md += [f"  - `{target_of(m)}` {m['observation']} `{m['id']}`" for m in members]
        questions = [text for text, members in open_groups if members[0]["pillar"] == p_]
        if questions:
            md += ["", "Open questions:", ""] + [f"- {q}" for q in questions]
        md.append("")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import next_steps  # noqa: E402
    md += ["### E. Reference material", ""] + next_steps.markdown(pathways_doc.get("next_steps") or {}, next_steps.labels(args.language).get("pathways", PATHWAYS), args.language)
    out = args.output or run / "06-assessment"
    out.mkdir(parents=True, exist_ok=True)
    for stale in (DETAILS_FILE, "details.md"):  # the two-page layout of earlier versions
        if (out / stale).is_file():
            (out / stale).unlink()
    (out / REPORT_FILE).write_text(plain_words(mask_people(html_doc)), encoding="utf-8")
    (out / "assessment.md").write_text(plain_words(mask_people("\n".join(md))), encoding="utf-8")
    (out / "assessment-summary.json").write_text(json.dumps({
        "schema_version": "1.2", "run_id": inventory["run_id"], "account_id": inventory["account_id"], "workload": args.workload, "status": status,
        "language": "en", "files": {"report": REPORT_FILE, "markdown": "assessment.md"},
        "sections": [pid for pid, _ in PAGES],
        "grades": {p: {"grade": grades[p][0], **{k: v for k, v in grades[p][1].items() if k != "drivers"}, "drivers": [c["rule_id"] for c in grades[p][1]["drivers"]]} for p in PILLARS},
        "key_issues": issues, "iac_changes": changes, "diagram_coverage": (diagram_audit or {}).get("coverage", {}).get("status", "not_run"),
        "imported_findings": {"count": len(screener), "tool": "Service Screener" if upstream_manifest else None,
                              "input_sha256": upstream_manifest.get("input_sha256") if upstream_manifest else None},
        "recommended_directions": [c["pathway"] for c in recommended], "wall_clock_minutes": run_minutes(inventory),
        "specialists": [{"specialist": sp["specialist"], "role": sp["role"], "recommendations": len(sp.get("recommendations") or []),
                         "pathways": {p: statuses.get(p) for p in sp.get("pathways", [])}, "diagram": (sp.get("diagram") or {}).get("svg")} for sp in specialists],
        "run_metadata": run_meta}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if BENEFITS["result"]:
        (out / "benefits.json").write_text(json.dumps(BENEFITS["result"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "iac-changes.json").write_text(json.dumps({"schema_version": "1.0", "run_id": inventory["run_id"], "account_id": inventory["account_id"],
                                                       "changes": changes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "generated", "report_status": status, "output": str(out), "key_issues": len(issues), "imported_findings": len(screener),
            "iac_changes": len(changes), "diagram_coverage": (diagram_audit or {}).get("coverage", {}).get("status", "not_run"),
            "grades": {p: grades[p][0] for p in PILLARS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--key-issues", type=Path)
    parser.add_argument("--screener", type=Path, help="screener-findings.json from import_service_screener.py (shown as ordinary security items)")
    parser.add_argument("--diagram-audit", type=Path, help="diagram-audit.json; default 04-review/diagrams/diagram-audit.json. Final status requires coverage pass")
    parser.add_argument("--language", default="en", help="language of the catalog-backed Next steps text (en, ko); the rest of the report is localized by localize_artifacts.py")
    parser.add_argument("--run-metadata", type=Path)
    parser.add_argument("--as-is-diagram", type=Path)
    parser.add_argument("--to-be-diagram", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wall-clock-minutes", type=int, help="minutes from run start to the end of the assessment; default: from the "
                        "run id to the newest file in the stage folders 01- to 05-")
    args = parser.parse_args()
    try:
        print(json.dumps(render(args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Assessment report stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
