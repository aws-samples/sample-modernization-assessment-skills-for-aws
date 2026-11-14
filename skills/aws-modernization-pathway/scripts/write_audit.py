#!/usr/bin/env python3
"""After a run, check CloudTrail for any change the assessment's principal made. Python stdlib only; read-only.

  python3 write_audit.py --run-dir RUN_DIR

Reads the principal, account and start time that stage 1 recorded (01-setup/access-check.json) and the profile and
regions in scope.json, then lists the run's CloudTrail management events with ReadOnly=false in each region (plus
us-east-1, where global services such as IAM log) with `cloudtrail lookup-events`. Events by that principal are kept;
SSM SendCommand is expected when host inspection was enabled. The result, RUN_DIR/write-audit.json, is `clean`,
`writes_found` or `unavailable` (a region could not be read), and the report shows it under Method and provenance.

CloudTrail can take up to 15 minutes to show an event, so a run's last minutes may be missing; rerun the script later
to cover them. Data events (object-level S3, Lambda invoke) are not in lookup-events and are not checked here.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("audit_collect", HERE.parent.parent / "aws-as-is-analysis/scripts/collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

MAX_PAGES = 20
DELAY_NOTE = "CloudTrail can take up to 15 minutes to show an event; rerun write_audit.py later to cover the run's last minutes."


def lookup(region, start, end, profile, env, timeout):
    """All ReadOnly=false management events in one region and window, or an error code."""
    events, token = [], None
    for _ in range(MAX_PAGES):
        argv = ["cloudtrail", "lookup-events", "--region", region, "--lookup-attributes", "AttributeKey=ReadOnly,AttributeValue=false",
                "--start-time", start, "--end-time", end, "--max-results", "50", "--output", "json", "--no-cli-pager"]
        if token:
            argv += ["--next-token", token]
        if profile:
            argv += ["--profile", profile]
        response = subprocess.run(["aws", *collect.checked_arguments(argv)], capture_output=True, text=True,  # nosec B603 B607 - see checked_arguments
                                  env=env, timeout=timeout)
        if response.returncode:
            return events, collect_error(response.stderr)
        page = json.loads(response.stdout or "{}")
        events += page.get("Events", [])
        token = page.get("NextToken")
        if not token:
            return events, None
    return events, "page_limit_reached"


def collect_error(stderr):
    match = re.search(r"\(([A-Za-z0-9_.-]+)\)", stderr or "")
    return match.group(1) if match else "cli_or_network_error"


def by_principal(event, principal_arn):
    try:
        detail = json.loads(event.get("CloudTrailEvent") or "{}")
    except ValueError:
        return False
    return (detail.get("userIdentity") or {}).get("arn") == principal_arn


def audit(run, timeout=60):
    access = json.loads((run / "01-setup/access-check.json").read_text(encoding="utf-8"))
    scope = json.loads((run / "scope.json").read_text(encoding="utf-8"))
    principal, start = access.get("principal_arn"), access.get("checked_at")
    if not principal or not start:
        raise ValueError("access-check.json has no principal_arn or checked_at")
    profile = (scope.get("auth") or {}).get("profile")
    regions = sorted(set(scope.get("regions") or []) | {"us-east-1"})
    expected = {("ssm.amazonaws.com", "SendCommand")} if (scope.get("host_inspection") or {}).get("enabled") else set()
    end = datetime.now(timezone.utc).isoformat()
    env = collect.environment(profile)
    result = {"schema_version": "1.0", "principal_arn": principal, "account_id": access.get("account_id"),
              "window": {"start": start, "end": end}, "regions": {}, "expected": [], "unexpected": [], "note": DELAY_NOTE}
    for region in regions:
        events, error = lookup(region, start, end, profile, env, timeout)
        mine = [e for e in events if by_principal(e, principal)]
        result["regions"][region] = {"status": "error" if error else "ok", "error_code": error, "write_events_in_account": len(events),
                                     "by_principal": len(mine)}
        for event in mine:
            item = {"time": str(event.get("EventTime")), "source": event.get("EventSource"), "name": event.get("EventName"), "region": region}
            (result["expected"] if (item["source"], item["name"]) in expected else result["unexpected"]).append(item)
    failed = [r for r, v in result["regions"].items() if v["status"] != "ok"]
    result["status"] = "writes_found" if result["unexpected"] else ("unavailable" if failed else "clean")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        result = audit(args.run_dir.resolve(), args.timeout)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "unusable", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    (args.run_dir / "write-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "unexpected": len(result["unexpected"]), "expected": len(result["expected"]),
                      "regions": {r: v["status"] for r, v in result["regions"].items()}, "output": str(args.run_dir / "write-audit.json")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
