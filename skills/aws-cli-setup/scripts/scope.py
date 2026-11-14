#!/usr/bin/env python3
"""Write and validate scope.json, the one file every later stage reads to know what to assess.

The agent fills the values from the interview (or from the user's prompt when it already named
them) and calls `write`; `validate` is what collect.py, review.py and assessment_report.py call
before trusting the scope. No AWS calls.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

SCHEMA = "1.0"
SCOPE_TYPES = ("account", "stack", "tag")
ENVIRONMENTS = ("production", "non-production", "unknown")
OPERATORS = ("internal", "provider", "mixed", "unknown")
SUPPORT_PLANS = ("basic", "developer", "business", "enterprise-on-ramp", "enterprise", "unknown")
SOURCES = ("interview", "prompt", "defaults")
DEFAULT_SERVICES = ("ec2,elb,elbv2,autoscaling,rds,ecs,eks,lambda,s3,dynamodb,cloudwatch,cloudtrail,iam,ssm,acm,apigateway,apigatewayv2,wafv2,"
                    "directconnect,route53,cloudfront,securityhub,guardduty,config,inspector2,accessanalyzer,computeoptimizer,support,health,organizations")
# Mirrors collect.py DEFAULT_SERVICES; discovery adds optional listings (ElastiCache, SQS, Glue, Bedrock, ...) when it finds them.
REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
ACCOUNT = re.compile(r"^\d{12}$")
INSTANCE = re.compile(r"^i-[0-9a-f]{8,17}$")


def validate(doc):
    """Return a list of problems; empty means valid."""
    problems = []
    if doc.get("schema_version") != SCHEMA:
        problems.append("schema_version must be '1.0'")
    if not ACCOUNT.match(str(doc.get("account_id", ""))):
        problems.append("account_id must be 12 digits")
    regions = doc.get("regions")
    if not isinstance(regions, list) or not regions or not all(REGION.match(str(r)) for r in regions):
        problems.append("regions must be a non-empty list of region codes")
    if not isinstance(doc.get("workload_name"), str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", doc.get("workload_name", "")):
        problems.append("workload_name must be 1-64 chars of letters, digits, . _ -")
    wl = doc.get("workload") or {}
    kind = wl.get("type")
    if kind not in SCOPE_TYPES:
        problems.append(f"workload.type must be one of {SCOPE_TYPES}")
    elif kind == "stack" and not (isinstance(wl.get("stack_names"), list) and wl["stack_names"]):
        problems.append("workload.stack_names required for type=stack")
    elif kind == "tag":
        tags = wl.get("tags")
        if not isinstance(tags, dict) or not tags or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
            problems.append("workload.tags must be a non-empty {key: value} map for type=tag")
    if not isinstance(wl.get("exclude_stacks", []), list):
        problems.append("workload.exclude_stacks must be a list")
    ctx = doc.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict):
            problems.append("context must be an object")
        else:
            for key, allowed in (("environment", ENVIRONMENTS), ("operator", OPERATORS), ("support_plan", SUPPORT_PLANS)):
                if ctx.get(key, "unknown") not in allowed:
                    problems.append(f"context.{key} must be one of {', '.join(allowed)}")
            comp = ctx.get("compliance", ["unknown"])
            if not isinstance(comp, list) or not all(isinstance(c, str) and re.match(r"^[A-Za-z0-9][\w .:/()-]{0,40}$", c) for c in comp):
                problems.append("context.compliance must be a list of short names (ISMS-P, PCI DSS ...), ['none'] or ['unknown']")
    hi = doc.get("host_inspection") or {}
    if not isinstance(hi.get("enabled"), bool):
        problems.append("host_inspection.enabled must be boolean")
    elif hi["enabled"]:
        ids = hi.get("instance_ids")
        if not isinstance(ids, list) or not ids or not all(INSTANCE.match(str(i)) for i in ids):
            problems.append("host_inspection.instance_ids must list i-... ids when enabled")
    if not isinstance(doc.get("services"), list) or not doc.get("services"):
        problems.append("services must be a non-empty list")
    if not isinstance(doc.get("output_language"), str) or not doc.get("output_language"):
        problems.append("output_language required (e.g. 'ko', 'en')")
    auth = doc.get("auth") or {}
    if auth.get("mode") not in ("profile", "environment"):
        problems.append("auth.mode must be profile or environment")
    if auth.get("mode") == "profile" and not auth.get("profile"):
        problems.append("auth.profile required when auth.mode=profile")
    for key in ("access_key", "secret", "token", "password"):
        if any(key in k.lower() for k in auth):
            problems.append("auth must not contain credential values")
    if doc.get("confirmed_by") not in SOURCES:
        problems.append(f"confirmed_by must be one of {SOURCES}")
    if not isinstance(doc.get("confirmed_at"), str):
        problems.append("confirmed_at (ISO-8601) required")
    return problems


def build(args):
    workload = {"type": args.type, "exclude_stacks": args.exclude_stack or []}
    if args.type == "stack":
        workload["stack_names"] = args.stack or []
    if args.type == "tag":
        workload["tags"] = dict(kv.split("=", 1) for kv in (args.tag or []))
    doc = {
        "schema_version": SCHEMA,
        "account_id": args.account,
        "regions": [r.strip() for r in args.regions.split(",") if r.strip()],
        "workload_name": args.workload_name,
        "workload": workload,
        "services": [s.strip() for s in (args.services or DEFAULT_SERVICES).split(",") if s.strip()],
        "host_inspection": {"enabled": bool(args.inspect_instance), "instance_ids": args.inspect_instance or []},
        "output_language": args.language,
        "auth": {"mode": "profile", "profile": args.profile} if args.profile else {"mode": "environment"},
        "confirmed_by": args.confirmed_by,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "notes": args.note or [],
        "context": {"environment": args.environment_type or "unknown", "operator": args.operator or "unknown", "support_plan": args.support_plan or "unknown",
                    "compliance": [c.strip() for c in (args.compliance or "").split(",") if c.strip()] or ["unknown"]},
    }
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="write scope.json from confirmed answers")
    w.add_argument("--account", required=True)
    w.add_argument("--regions", required=True, help="comma-separated")
    w.add_argument("--workload-name", required=True)
    w.add_argument("--type", choices=SCOPE_TYPES, required=True)
    w.add_argument("--stack", action="append", help="repeatable; required for --type stack")
    w.add_argument("--tag", action="append", help="Key=Value, repeatable; required for --type tag")
    w.add_argument("--exclude-stack", action="append")
    w.add_argument("--services", help=f"comma-separated (default: {DEFAULT_SERVICES})")
    w.add_argument("--inspect-instance", action="append", help="i-... repeatable; enables host inspection")
    w.add_argument("--language", required=True)
    auth = w.add_mutually_exclusive_group(required=True)
    auth.add_argument("--profile")
    auth.add_argument("--environment", action="store_true")
    w.add_argument("--environment-type", choices=ENVIRONMENTS, help="what the workload is for; the report stops asking when this is known")
    w.add_argument("--operator", choices=OPERATORS, help="who runs the workload day to day")
    w.add_argument("--support-plan", choices=SUPPORT_PLANS, help="the account's AWS Support plan as the owner states it")
    w.add_argument("--compliance", help="comma-separated obligations the workload must show (ISMS-P, PCI DSS, SOC 2 ...), or 'none'")
    w.add_argument("--confirmed-by", choices=SOURCES, required=True)
    w.add_argument("--note", action="append")
    w.add_argument("--output", type=Path, required=True)
    v = sub.add_parser("validate", help="validate an existing scope.json")
    v.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "validate":
        doc = json.loads(args.path.read_text(encoding="utf-8"))
        problems = validate(doc)
        print(json.dumps({"status": "valid" if not problems else "invalid", "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1
    if args.output.exists():
        parser.error("output exists; use a new path")
    doc = build(args)
    problems = validate(doc)
    if problems:
        print(json.dumps({"status": "invalid", "problems": problems}, ensure_ascii=False), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "written", "output": str(args.output), "workload": doc["workload"],
                      "regions": doc["regions"], "host_inspection": doc["host_inspection"]["enabled"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
