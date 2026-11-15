#!/usr/bin/env python3
"""Read-only look around an account so the scope interview can offer real choices.

Writes `scope-options.json`: regions that hold resources, CloudFormation stacks with resource
counts, the most common tag keys/values, SSM-managed instances, and the identity used. Nothing is
estimated; a failed or denied query is recorded as such. Every call is describe/list/get only.

The interview itself (asking the user, writing scope.json) is done by the agent following
aws-cli-setup; this script only gathers the facts and proposes defaults.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def checked_arguments(arguments):
    """Guard for every CLI invocation: list form only (no shell), the program name is a literal at the call site
    (`["aws", *checked_arguments(...)]`, resolved through PATH exactly as shutil.which resolved it), and every argument
    is a string without NUL or newline. Arguments come from our own tables, resource identifiers read from AWS
    responses, or validated CLI options."""
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("command arguments must be a non-empty list")
    for item in arguments:
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("command arguments must be strings without control characters")
    return list(arguments)

SCHEMA = "1.0"
# Calls run a few at a time: each CLI call takes about two seconds, and one by one a busy account took longer than some
# agent shells allow for a single command.
WORKERS = 8
# Regions enabled by default in most accounts; opt-in regions are only probed when --regions names them.
DEFAULT_REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-south-1", "ap-northeast-1",
                   "ap-northeast-2", "ap-northeast-3", "ap-southeast-1", "ap-southeast-2", "ca-central-1",
                   "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1", "sa-east-1")
STACK_STATES = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE")
TAG_KEYS_OF_INTEREST = ("Environment", "environment", "env", "Project", "project", "Application", "application",
                        "app", "Service", "service", "Owner", "owner", "Team", "team", "Workload", "workload",
                        "Name", "aws:cloudformation:stack-name")


def now():
    return datetime.now(timezone.utc).isoformat()


def environment(profile):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AWS_ENDPOINT_URL"):
            env.pop(key)
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off", AWS_EC2_METADATA_DISABLED="true",
               AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true", AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="2")
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_DEFAULT_PROFILE", None)
    if profile:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
            env.pop(key, None)
    return env


class Discovery:
    def __init__(self, args):
        self.args = args
        self.env = environment(args.profile)
        self.aws = shutil.which("aws")
        if not self.aws:
            raise ValueError("AWS CLI v2 missing")
        self.calls = []

    def call(self, service, operation, region, *arguments, query=None, timeout=None):
        """One read-only CLI invocation; returns (data, status, error_code). Never raises on AWS errors."""
        if not re.match(r"^(describe|list|get)-", operation):
            raise ValueError("scope discovery only runs describe/list/get operations")
        argv = [service, operation, *arguments, "--output", "json", "--no-cli-pager"]
        if region:
            argv += ["--region", region]
        if self.args.profile:
            argv += ["--profile", self.args.profile]
        if query:
            argv += ["--query", query]
        record = {"service": service, "operation": operation, "region": region, "status": "ok", "error_code": None}
        self.calls.append(record)
        try:
            response = subprocess.run(["aws", *checked_arguments(argv)], capture_output=True, text=True, env=self.env,  # nosec B603 B607 - see checked_arguments
                                      timeout=timeout or self.args.timeout)
        except subprocess.TimeoutExpired:
            record.update(status="error", error_code="timeout")
            return None, "error", "timeout"
        if response.returncode:
            match = re.search(r"\(([A-Za-z0-9_.-]+)\)", response.stderr)
            code = match.group(1) if match else "cli_or_network_error"
            status = "access_denied" if ("AccessDenied" in code or "Unauthorized" in code) else "error"
            if code in {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"}:
                status = "expired_token"
            record.update(status=status, error_code=code)
            return None, status, code
        text = response.stdout.strip()
        if not text:
            return None, "ok", None
        try:
            return json.loads(text), "ok", None
        except json.JSONDecodeError:
            record.update(status="error", error_code="invalid_json")
            return None, "error", "invalid_json"

    def identity(self):
        data, status, code = self.call("sts", "get-caller-identity", None)
        if status != "ok" or not data:
            raise ValueError(f"cannot establish identity ({code})")
        return {"account_id": data["Account"], "arn": data["Arn"]}

    def regions_with_resources(self, candidates):
        """Resource Groups Tagging API lists resource ARNs per region (the CLI paginates; --query disables --max-items)."""
        def probe(region):
            data, status, code = self.call("resourcegroupstaggingapi", "get-resources", region,
                                           "--resources-per-page", "100",
                                           query="length(ResourceTagMappingList)")
            if status == "ok":
                return {"status": "ok", "resources": int(data) if isinstance(data, int) else 0}
            return {"status": status, "error_code": code}
        return dict(zip(candidates, parallel(probe, candidates)))

    def stacks(self, region):
        data, status, code = self.call("cloudformation", "list-stacks", region,
                                       "--stack-status-filter", *STACK_STATES,
                                       query="StackSummaries[].{name:StackName,status:StackStatus,parent:ParentId}")
        if status != "ok":
            return {"status": status, "error_code": code, "items": []}
        def describe(stack):
            resources, rstatus, _ = self.call("cloudformation", "list-stack-resources", region,
                                              "--stack-name", stack["name"],
                                              query="StackResourceSummaries[].ResourceType")
            types = Counter(resources or []) if rstatus == "ok" else Counter()
            return {"name": stack["name"], "status": stack["status"],
                    "resource_count": sum(types.values()) if rstatus == "ok" else None,
                    "top_types": [t for t, _ in types.most_common(5)]}
        # Nested stacks are counted under their parent.
        top = [s for s in (data or [])[: self.args.max_stacks] if not s.get("parent")]
        return {"status": "ok", "items": parallel(describe, top)}

    def tags(self, region):
        data, status, code = self.call("resourcegroupstaggingapi", "get-resources", region,
                                       "--resources-per-page", "100",
                                       query="ResourceTagMappingList[].Tags[]")
        if status != "ok":
            return {"status": status, "error_code": code, "keys": []}
        pairs = Counter((t["Key"], t["Value"]) for t in (data or []) if t.get("Key") and not t["Key"].startswith("aws:"))
        keys = Counter(k for k, _ in pairs.elements())
        result = []
        for key, count in keys.most_common(self.args.max_tag_keys):
            values = [{"value": v, "resources": n} for (k, v), n in pairs.most_common() if k == key][:8]
            result.append({"key": key, "resources": count, "values": values})
        # Surface the conventional keys first when present, then the rest by frequency.
        result.sort(key=lambda r: (r["key"] not in TAG_KEYS_OF_INTEREST, -r["resources"]))
        return {"status": "ok", "keys": result}

    def ssm_instances(self, region):
        data, status, code = self.call("ssm", "describe-instance-information", region,
                                       query="InstanceInformationList[].{id:InstanceId,ping:PingStatus,platform:PlatformName}")
        if status != "ok":
            return {"status": status, "error_code": code, "online": 0, "instances": []}
        items = data or []
        return {"status": "ok", "online": sum(1 for i in items if i.get("ping") == "Online"),
                "instances": [{"id": i["id"], "ping": i.get("ping"), "platform": i.get("platform")} for i in items[:50]]}


def parallel(function, items):
    """function(item) for every item, in order, at most WORKERS at a time."""
    items = list(items)
    if len(items) < 2:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        return list(pool.map(function, items))


def propose_defaults(regions, stacks_by_region):
    """Defaults the interview offers when the user does not say otherwise."""
    active = [r for r, v in regions.items() if v.get("status") == "ok" and v.get("resources", 0) > 0]
    all_stacks = [(r, s) for r, block in stacks_by_region.items() for s in block.get("items", [])]
    if len(all_stacks) == 1:
        region, stack = all_stacks[0]
        scope = {"type": "stack", "stack_name": stack["name"], "reason": "the only CloudFormation stack in scope"}
    else:
        scope = {"type": "account", "reason": "no single stack or tag identifies one workload; the user should narrow it"}
    return {"regions": active or list(regions), "workload": scope, "host_inspection": False,
            "language": "the language of the user's request"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--profile")
    auth.add_argument("--environment", action="store_true")
    parser.add_argument("--expected-account", help="Stop if the caller identity is a different account")
    parser.add_argument("--regions", help="Comma-separated regions to probe (default: commonly enabled regions)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-stacks", type=int, default=25)
    parser.add_argument("--max-tag-keys", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=40)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; use a new path")
    started = now()
    try:
        disco = Discovery(args)
        who = disco.identity()
    except ValueError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    if args.expected_account and who["account_id"] != args.expected_account:
        print(json.dumps({"status": "failed", "reason": "account_mismatch"}), file=sys.stderr)
        return 1
    candidates = [r.strip() for r in args.regions.split(",")] if args.regions else list(DEFAULT_REGIONS)
    regions = disco.regions_with_resources(candidates)
    active = [r for r, v in regions.items() if v.get("status") == "ok" and v.get("resources", 0) > 0]
    stacks = dict(zip(active, parallel(disco.stacks, active)))
    tags = dict(zip(active, parallel(disco.tags, active)))
    ssm = dict(zip(active, parallel(disco.ssm_instances, active)))
    result = {
        "schema_version": SCHEMA, "discovered_at": started, "completed_at": now(), "read_only": True,
        "identity": who, "auth_mode": "profile" if args.profile else "environment",
        "regions": regions, "stacks": stacks, "tags": tags, "ssm_managed_instances": ssm,
        "api_calls": len(disco.calls),
        "denied_or_failed": [c for c in disco.calls if c["status"] != "ok"],
        "proposed_defaults": propose_defaults(regions, stacks),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "discovered", "account_id": who["account_id"], "active_regions": active,
                      "stacks": sum(len(b["items"]) for b in stacks.values()),
                      "denied_or_failed": len(result["denied_or_failed"]), "output": str(args.output)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
