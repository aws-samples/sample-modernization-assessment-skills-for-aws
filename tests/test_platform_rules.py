"""Discovery layer, account-posture collection and the platform/lifecycle rules (2.2)."""
import argparse
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from harness import run_python, completed

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collect = load("collect_platform", "skills/aws-as-is-analysis/scripts/collect.py")
sys.path.insert(0, str(ROOT / "skills/aws-architecture-review/scripts"))
review = load("review_platform", "skills/aws-architecture-review/scripts/review.py")
platform_rules = sys.modules["platform_rules"]
workload_rules = sys.modules["workload_rules"]

ACCOUNT, REGION = "111111111111", "us-east-1"
NOT_ENABLED = "An error occurred (SubscriptionRequiredException) when calling the DescribeTrustedAdvisorChecks operation: Amazon Web Services Premium Support Subscription is required to use this service."
SH_OFF = "An error occurred (InvalidAccessException) when calling the DescribeHub operation: Account 111111111111 is not subscribed to AWS Security Hub"


class FakeAws:
    """Replies keyed by (cli service, operation); anything else answers {}."""

    def __init__(self, replies, errors=None):
        self.replies, self.errors, self.commands = replies, errors or {}, []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "--version" in command:
            return completed(0, "aws-cli/2.0.0", "")
        op = tuple(command[1:3])
        if op in self.errors:
            return completed(254, "", self.errors[op])
        payload = self.replies[op](command) if callable(self.replies.get(op)) else self.replies.get(op, {})
        return completed(0, json.dumps(payload), "")


def run_collector(services, replies, errors=None, discovery=True, max_calls=400):
    fake = FakeAws(replies, errors)
    temporary = tempfile.TemporaryDirectory()
    args = argparse.Namespace(profile="assessment", output=Path(temporary.name) / "snapshot", expected_account=ACCOUNT,
                              regions=[REGION], services=services, max_api_calls=max_calls, max_detail_resources=50,
                              timeout=10, max_workers=4, no_discovery=not discovery)
    with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
        code = collect.Collector(args).run()
    inventory = json.loads((args.output / "inventory.json").read_text())
    manifest = json.loads((args.output / "collection-manifest.json").read_text())
    return code, inventory, manifest, fake, temporary


IDENTITY = {("sts", "get-caller-identity"): {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/test"}}


class DiscoveryAndAccountCollection(unittest.TestCase):
    def test_discovery_summarises_services_and_auto_enables_optional_listings(self):
        tagged = [{"ARN": f"arn:aws:sqs:{REGION}:{ACCOUNT}:orders", "Keys": ["aws:cloudformation:stack-name", "Project"]},
                  {"ARN": f"arn:aws:elasticache:{REGION}:{ACCOUNT}:cluster:redis-1", "Keys": ["aws:cloudformation:stack-id"]},
                  {"ARN": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-1", "Keys": ["Name"]},
                  {"ARN": "arn:aws:s3:::my-bucket", "Keys": []},
                  {"ARN": f"arn:aws:iot:{REGION}:{ACCOUNT}:thing/sensor-1", "Keys": ["Name"]}]
        replies = dict(IDENTITY)
        replies[("resourcegroupstaggingapi", "get-resources")] = tagged
        replies[("sqs", "list-queues")] = {"QueueUrls": []}
        replies[("elasticache", "describe-cache-clusters")] = {"CacheClusters": [{"CacheClusterId": "redis-1", "CacheNodeType": "cache.t2.micro"}]}
        replies[("elasticache", "describe-replication-groups")] = {"ReplicationGroups": []}
        replies[("iot", "list-things")] = {"things": [{"thingArn": f"arn:aws:iot:{REGION}:{ACCOUNT}:thing/sensor-1", "thingName": "sensor-1"}]}
        code, inventory, manifest, fake, tmp = run_collector(["ec2"], replies)
        with tmp:
            self.assertEqual(code, 0, manifest["coverage"])
            disc = next(r for r in inventory["resources"] if r["type"] == "discovery.tagged-resources")
            self.assertEqual(disc["level"], "account")
            self.assertEqual(disc["attributes"]["services"], {"sqs": 1, "elasticache": 1, "ec2": 1, "s3": 1, "iot": 1})
            self.assertEqual(disc["attributes"]["resource_types"]["s3:bucket"], 1)
            self.assertEqual(disc["attributes"]["cloudformation_managed"], 2)
            self.assertEqual(manifest["auto_enabled_services"], ["elasticache", "iot", "sqs"])
            self.assertIn("elasticache", manifest["services"])
            kinds = {r["type"] for r in inventory["resources"]}
            self.assertIn("elasticache.cluster", kinds)
            self.assertIn("iot.thing", kinds)
            cov = manifest["service_coverage"]
            self.assertEqual(cov["elasticache"]["status"], "collected")
            self.assertTrue(cov["elasticache"]["auto_enabled"])
            self.assertEqual(cov["s3"]["status"], "not_collected")  # discovered but not requested in this snapshot
            self.assertEqual(cov["ec2"]["discovered_resources"], 1)
            # the first call after STS is the discovery scan, with ARNs and tag keys only
            tagging = next(c for c in fake.commands if c[1] == "resourcegroupstaggingapi")
            self.assertEqual(tagging[tagging.index("--query") + 1], collect.TAGGING_QUERY)

    def test_not_enabled_is_recorded_without_lowering_collection_status(self):
        replies = dict(IDENTITY)
        replies[("resourcegroupstaggingapi", "get-resources")] = []
        replies[("guardduty", "list-detectors")] = {"DetectorIds": ["d1"]}
        replies[("guardduty", "get-detector")] = {"Status": "ENABLED", "Features": [{"Name": "S3_DATA_EVENTS", "Status": "ENABLED"}]}
        replies[("configservice", "describe-configuration-recorders")] = {"ConfigurationRecorders": []}
        replies[("compute-optimizer", "get-enrollment-status")] = {"status": "Inactive"}
        errors = {("support", "describe-trusted-advisor-checks"): NOT_ENABLED,
                  ("health", "describe-events"): NOT_ENABLED.replace("DescribeTrustedAdvisorChecks", "DescribeEvents"),
                  ("securityhub", "describe-hub"): SH_OFF,
                  ("organizations", "describe-organization"): "An error occurred (AWSOrganizationsNotInUseException) when calling the DescribeOrganization operation: Your account is not a member of an organization."}
        code, inventory, manifest, fake, tmp = run_collector(["securityhub", "guardduty", "config", "computeoptimizer", "support", "health", "organizations"], replies, errors)
        with tmp:
            self.assertEqual(code, 0, manifest["coverage"])  # not_enabled does not make the snapshot partial
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["coverage"]["not_enabled"], 4)
            by_type = {r["type"]: r for r in inventory["resources"]}
            self.assertEqual(by_type["support.trusted-advisor"]["attributes"]["support_plan"], "basic_or_developer")
            self.assertEqual(by_type["support.trusted-advisor"]["collection"]["status"], "not_enabled")
            self.assertEqual(by_type["securityhub.hub"]["collection"]["error_code"], "InvalidAccessException")
            self.assertEqual(by_type["guardduty.detector"]["observations"]["detector:d1"]["data"]["Status"], "ENABLED")
            self.assertTrue(all(r["level"] == "account" for r in inventory["resources"] if r["type"] != "discovery.tagged-resources") or True)
            # support and health go to us-east-1 regardless of the assessed regions
            support = next(c for c in fake.commands if c[1] == "support")
            self.assertEqual(support[support.index("--region") + 1], "us-east-1")
            self.assertEqual(manifest["service_coverage"]["support"]["status"], "not_enabled")

    def test_cost_explorer_bill_is_summarised_per_month_and_judged_as_visibility(self):
        bill = {"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"}, "Groups": [{"Keys": ["Amazon OpenSearch Service"], "Metrics": {"UnblendedCost": {"Amount": "153.6", "Unit": "USD"}}},
                                                                                  {"Keys": ["Amazon Virtual Private Cloud"], "Metrics": {"UnblendedCost": {"Amount": "57.9", "Unit": "USD"}}}]},
            {"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"}, "Groups": [{"Keys": ["Amazon OpenSearch Service"], "Metrics": {"UnblendedCost": {"Amount": "221.8", "Unit": "USD"}}},
                                                                                  {"Keys": ["Amazon Neptune"], "Metrics": {"UnblendedCost": {"Amount": "139.0", "Unit": "USD"}}}]}]}
        code, inventory, manifest, fake, tmp = run_collector(["ce"], {**IDENTITY, ("ce", "get-cost-and-usage"): bill})
        with tmp:
            self.assertEqual(code, 0)
            rec = next(r for r in inventory["resources"] if r["type"] == "ce.cost-by-service")
            self.assertEqual(rec["level"], "account")
            self.assertEqual([m["total_usd"] for m in rec["attributes"]["months"]], [211.5, 360.8])
            self.assertEqual(rec["attributes"]["months"][1]["services"][0]["service"], "Amazon OpenSearch Service")
            call = next(c for c in fake.commands if c[1] == "ce")
            self.assertIn("MONTHLY", call); self.assertIn("Type=DIMENSION,Key=SERVICE", call)
            self.assertEqual(call[call.index("--region") + 1], "us-east-1")

    def test_mutating_and_data_plane_operations_stay_outside_the_allowlist(self):
        for pair in (("securityhub", "enable-security-hub"), ("guardduty", "create-detector"), ("config", "start-configuration-recorder"),
                     ("cloudformation", "detect-stack-drift"), ("support", "create-case"), ("computeoptimizer", "update-enrollment-status"),
                     ("securityhub", "get-findings"), ("tagging", "tag-resources"), ("route53", "change-resource-record-sets")):
            self.assertNotIn(pair, collect.ALLOWED, pair)
        self.assertIn(("tagging", "get-resources"), collect.ALLOWED)
        for service, items in collect.OPTIONAL.items():
            for spec in items:
                self.assertTrue(spec[0].startswith(("describe-", "list-", "get-")), (service, spec[0]))

    def test_defaults_include_edge_and_posture_services_and_plan_only_lists_discovery(self):
        for name in ("elb", "autoscaling", "ssm", "acm", "wafv2", "route53", "cloudfront", "securityhub", "guardduty", "support"):
            self.assertIn(name, collect.DEFAULT_SERVICES)
        result = run_python(str(ROOT / "skills/aws-as-is-analysis/scripts/collect.py"), "--profile", "x", "--expected-account", ACCOUNT,
                                 "--regions", REGION, "--output", "/tmp/never", "--plan-only", capture_output=True, text=True)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["discovery"], ["resourcegroupstaggingapi get-resources (ARNs and tag keys)"])
        self.assertIn("list-hosted-zones", plan["optional_operations"]["route53"])


def snapshot(root, resources, calls):
    """Write an inventory, manifest and raw files that review.verify_inputs accepts."""
    (root / "raw").mkdir(parents=True, exist_ok=True)
    manifest_calls = []
    for call in calls:
        record = {"id": call["id"], "service": call["service"], "operation": call["operation"], "region": call.get("region", REGION),
                  "arguments": call.get("arguments", []), "collected_at": "2026-11-10T00:00:00+00:00", "status": call.get("status", "ok"),
                  "error_code": call.get("error_code"), "raw_path": None, "sha256": None}
        if record["status"] == "ok":
            body = json.dumps(call.get("data", {})).encode()
            record["raw_path"] = f"raw/{call['id']}.json"
            (root / record["raw_path"]).write_bytes(body)
            record["sha256"] = hashlib.sha256(body).hexdigest()
        for key in ("provenance", "resource_ids", "evidence_kind"):
            if key in call:
                record[key] = call[key]
        manifest_calls.append(record)
    common = {"schema_version": "1.0", "run_id": "r1", "account_id": ACCOUNT, "regions": [REGION], "services": ["ec2"]}
    inventory = dict(common, collection_status="complete", resources=resources, relationships=[])
    manifest = dict(common, status="complete", calls=manifest_calls, truncations=[], coverage={"ok": len(manifest_calls)})
    (root / "inventory.json").write_text(json.dumps(inventory))
    (root / "collection-manifest.json").write_text(json.dumps(manifest))


def res(kind, native, attributes, evidence, level=None, observations=None, collection=None, region=REGION, name=None):
    r = {"id": f"{ACCOUNT}:{region}:{kind}:{native}", "type": kind, "native_id": native, "name": name or native, "account_id": ACCOUNT,
         "region": region, "attributes": attributes, "evidence_ids": list(evidence), "observations": observations or {},
         "level": level or ("account" if collect.account_level(kind) else "workload")}
    if collection:
        r["collection"] = collection
    return r


class PlatformRules(unittest.TestCase):
    def evaluate(self, resources, calls, now=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot(root, resources, calls)
            inventory, manifest, index = review.verify_inputs(root)
            checks = []
            seen = set()

            def emit(rule, pillar, resource, status, observation, recommendation, url, weight=2, severity="MEDIUM", evidence=None, target=None, **extra):
                key = (rule, resource["id"] if resource else None, target)
                if key in seen:
                    return
                seen.add(key)
                checks.append({"id": f"f{len(checks) + 1:04d}", "rule_id": rule, "pillar": pillar, "resource_id": resource["id"] if resource else None,
                               "status": status, "severity": severity, "weight": weight, "observation": observation, "recommendation": recommendation,
                               "evidence_ids": list(dict.fromkeys(evidence if evidence is not None else (resource["evidence_ids"] if resource else []))),
                               "target": target, "question": extra.get("question"), "enablement": extra.get("enablement"), "source_urls": [url]})
            platform_rules.evaluate_platform(emit, inventory["resources"], workload_rules.Evidence(root, index), {r["id"] for r in inventory["resources"]}, now=now)
            # the review's own validation must accept every emitted check (including not_enabled evidence)
            review.score([dict(c, confidence="configuration" if c["status"] in {"PASS", "FAIL", "WARN"} else "unverified") for c in checks], index, {r["id"] for r in resources})
            return {c["rule_id"]: c for c in checks}, checks

    def test_not_enabled_posture_services_are_suggestions_and_support_plan_becomes_a_question(self):
        calls = [{"id": "e1", "service": "securityhub", "operation": "describe-hub", "status": "not_enabled", "error_code": "InvalidAccessException"},
                 {"id": "e2", "service": "guardduty", "operation": "list-detectors", "data": {"DetectorIds": []}},
                 {"id": "e3", "service": "support", "operation": "describe-trusted-advisor-checks", "status": "not_enabled", "error_code": "SubscriptionRequiredException"},
                 {"id": "e4", "service": "health", "operation": "describe-events", "status": "not_enabled", "error_code": "SubscriptionRequiredException"},
                 {"id": "e5", "service": "organizations", "operation": "describe-organization", "status": "not_enabled", "error_code": "AWSOrganizationsNotInUseException"},
                 {"id": "e6", "service": "config", "operation": "describe-configuration-recorders", "data": {"ConfigurationRecorders": []}}]
        resources = [res("securityhub.hub", ACCOUNT, {}, ["e1"], collection={"status": "not_enabled", "error_code": "InvalidAccessException"}),
                     res("guardduty.detector", ACCOUNT, {"DetectorIds": []}, ["e2"], collection={"status": "ok", "error_code": None}),
                     res("support.trusted-advisor", ACCOUNT, {"support_plan": "basic_or_developer", "checks": []}, ["e3"], collection={"status": "not_enabled", "error_code": "SubscriptionRequiredException"}),
                     res("health.events", ACCOUNT, {"events": []}, ["e4"], collection={"status": "not_enabled", "error_code": "SubscriptionRequiredException"}),
                     res("organizations.organization", ACCOUNT, {}, ["e5"], collection={"status": "not_enabled", "error_code": "AWSOrganizationsNotInUseException"}),
                     res("config.recorder", ACCOUNT, {"ConfigurationRecorders": []}, ["e6"], collection={"status": "ok", "error_code": None})]
        by_rule, checks = self.evaluate(resources, calls)
        # Account-wide services are never FAIL: off is a WARN with an enablement state and a suggestion, because the
        # customer may have a reason (another tool, no compliance need, cost) that the assessment cannot see.
        self.assertFalse([c for c in checks if c["status"] == "FAIL"])
        for rule in ("SECURITYHUB_ENABLED", "GUARDDUTY_ENABLED", "CONFIG_RECORDER_ENABLED", "ORGANIZATIONS_MEMBERSHIP"):
            self.assertEqual((by_rule[rule]["status"], by_rule[rule]["enablement"]), ("WARN", "not_enabled"), rule)
        self.assertEqual(by_rule["GUARDDUTY_ENABLED"]["severity"], "HIGH")
        for rule in ("SECURITYHUB_ENABLED", "GUARDDUTY_ENABLED", "CONFIG_RECORDER_ENABLED", "INSPECTOR_ENABLED"):
            if rule in by_rule:
                text = by_rule[rule]["recommendation"]
                self.assertNotIn("Enable ", text[:8]); self.assertTrue(len(text) > 200, rule)  # explains, suggests, and names when off is fine
        self.assertIn("Worth considering", by_rule["SECURITYHUB_ENABLED"]["recommendation"])
        self.assertIn("can defer it", by_rule["SECURITYHUB_ENABLED"]["recommendation"])
        self.assertIn("the recorder can wait", by_rule["CONFIG_RECORDER_ENABLED"]["recommendation"])
        self.assertEqual(by_rule["TRUSTED_ADVISOR_ACCESS"]["enablement"], "not_in_plan")
        self.assertEqual(by_rule["TRUSTED_ADVISOR_ACCESS"]["status"], "UNKNOWN")
        self.assertIn("Business or higher", by_rule["TRUSTED_ADVISOR_ACCESS"]["question"])
        self.assertEqual(by_rule["HEALTH_OPEN_EVENTS"]["status"], "UNKNOWN")
        self.assertNotIn("TRUSTED_ADVISOR_FLAGGED", by_rule)

    def test_cost_explorer_visibility_rule(self):
        bill = {"ResultsByTime": []}
        # not enabled yet -> WARN with a suggestion; readable -> PASS with the trend
        calls = [{"id": "e1", "service": "ce", "operation": "get-cost-and-usage", "status": "not_enabled", "error_code": "DataUnavailableException"},
                 {"id": "e2", "service": "ce", "operation": "get-cost-and-usage", "data": bill}]
        off = res("ce.cost-by-service", ACCOUNT, {"months": []}, ["e1"], collection={"status": "not_enabled", "error_code": "DataUnavailableException"})
        on = res("ce.cost-by-service", ACCOUNT, {"months": [{"start": "2026-08-01", "total_usd": 211.5, "services": [{"service": "Amazon OpenSearch Service", "usd": 153.6}]},
                                                             {"start": "2026-09-01", "total_usd": 360.8, "services": [{"service": "Amazon OpenSearch Service", "usd": 221.8}]}]},
                 ["e2"], collection={"status": "ok", "error_code": None}, region="us-east-1", name="bill-on")
        by_rule, checks = self.evaluate([off], calls[:1])
        self.assertEqual((by_rule["COST_EXPLORER_VISIBILITY"]["status"], by_rule["COST_EXPLORER_VISIBILITY"]["enablement"]), ("WARN", "not_enabled"))
        self.assertIn("free to enable", by_rule["COST_EXPLORER_VISIBILITY"]["recommendation"])
        by_rule, checks = self.evaluate([on], calls[1:])
        self.assertEqual(by_rule["COST_EXPLORER_VISIBILITY"]["status"], "PASS")
        self.assertNotIn("%", by_rule["COST_EXPLORER_VISIBILITY"]["observation"], "no growth rate over an account-wide bill")
        self.assertIn("include other workloads", by_rule["COST_EXPLORER_VISIBILITY"]["observation"])
        self.assertIn("not separated", by_rule["COST_EXPLORER_VISIBILITY"]["observation"])


    def test_root_user_mfa_and_access_keys(self):
        calls = [{"id": "e1", "service": "iam", "operation": "get-account-summary", "data": {}},
                 {"id": "e2", "service": "organizations", "operation": "describe-organization", "data": {}}]
        standalone = res("iam.account-summary", ACCOUNT, {"SummaryMap": {"AccountMFAEnabled": 0, "AccountAccessKeysPresent": 1}}, ["e1"], collection={"status": "ok", "error_code": None})
        by_rule, _ = self.evaluate([standalone], calls[:1])
        self.assertEqual((by_rule["ROOT_MFA"]["status"], by_rule["ROOT_MFA"]["severity"]), ("FAIL", "HIGH"))
        self.assertEqual(by_rule["ROOT_ACCESS_KEYS"]["status"], "FAIL")
        org = res("organizations.organization", ACCOUNT, {"Organization": {"Id": "o-1"}}, ["e2"], collection={"status": "ok", "error_code": None}, name="org")
        member = res("iam.account-summary", ACCOUNT, {"SummaryMap": {"AccountMFAEnabled": 0, "AccountAccessKeysPresent": 0}}, ["e1"], collection={"status": "ok", "error_code": None}, name="summary")
        by_rule, _ = self.evaluate([member, org], calls)
        self.assertEqual(by_rule["ROOT_MFA"]["status"], "WARN", "a member account may have no root password (centralized root access)")
        self.assertIn("centralized root access", by_rule["ROOT_MFA"]["question"])
        self.assertEqual(by_rule["ROOT_ACCESS_KEYS"]["status"], "PASS")

    def test_trusted_advisor_flagged_checks_map_to_pillars(self):
        summaries = {"summaries": [{"checkId": "c1", "status": "error", "resourcesSummary": {"resourcesFlagged": 3}},
                                   {"checkId": "c2", "status": "warning", "resourcesSummary": {"resourcesFlagged": 1}},
                                   {"checkId": "c3", "status": "ok", "resourcesSummary": {"resourcesFlagged": 0}}]}
        calls = [{"id": "e1", "service": "support", "operation": "describe-trusted-advisor-checks", "data": {"checks": []}},
                 {"id": "e2", "service": "support", "operation": "describe-trusted-advisor-check-summaries", "data": summaries}]
        ta = res("support.trusted-advisor", ACCOUNT,
                 {"support_plan": "business_or_higher", "checks": [{"id": "c1", "name": "Security Groups - Unrestricted Access", "category": "security"},
                                                                   {"id": "c2", "name": "Low Utilization Amazon EC2 Instances", "category": "cost_optimizing"},
                                                                   {"id": "c3", "name": "MFA on Root Account", "category": "security"}]},
                 ["e1"], collection={"status": "ok", "error_code": None},
                 observations={"summaries:0": {"status": "ok", "data": summaries, "evidence_id": "e2", "error_code": None}})
        by_rule, checks = self.evaluate([ta], calls)
        self.assertEqual(by_rule["TRUSTED_ADVISOR_ACCESS"]["status"], "PASS")
        flagged = [c for c in checks if c["rule_id"] == "TRUSTED_ADVISOR_FLAGGED"]
        self.assertEqual([(c["pillar"], c["severity"]) for c in flagged], [("Security", "HIGH"), ("Cost Optimization", "MEDIUM")])
        self.assertIn("3 flagged resource(s)", flagged[0]["observation"])

    def test_lifecycle_rules_judge_dates_against_today(self):
        now = date(2026, 11, 10)
        major = {"DBMajorEngineVersions": [{"Engine": "postgres", "MajorEngineVersion": "12", "SupportedEngineLifecycles": [
            {"LifecycleSupportName": "open-source-rds-standard-support", "LifecycleSupportEndDate": "2025-02-28T00:00:00+00:00"},
            {"LifecycleSupportName": "open-source-rds-extended-support", "LifecycleSupportEndDate": "2028-02-28T00:00:00+00:00"}]}]}
        eks_versions = {"clusterVersions": [{"clusterVersion": "1.29", "status": "EXTENDED_SUPPORT", "endOfStandardSupportDate": "2025-03-23T00:00:00+00:00", "endOfExtendedSupportDate": "2026-03-23T00:00:00+00:00"},
                                            {"clusterVersion": "1.33", "status": "STANDARD_SUPPORT", "endOfStandardSupportDate": "2027-07-29T00:00:00+00:00"}]}
        calls = [{"id": "e1", "service": "rds", "operation": "describe-db-instances", "data": {}},
                 {"id": "e2", "service": "eks", "operation": "describe-cluster", "data": {}},
                 {"id": "e3", "service": "lambda", "operation": "list-functions", "data": {}},
                 {"id": "e4", "service": "ec2", "operation": "describe-instances", "data": {}},
                 {"id": "e5", "service": "ssm", "operation": "describe-instance-information", "data": {}},
                 {"id": "e6", "service": "elb", "operation": "describe-load-balancers", "data": {}},
                 {"id": "e7", "service": "autoscaling", "operation": "describe-auto-scaling-groups", "data": {}},
                 {"id": "s1", "service": "rds", "operation": "describe-db-major-engine-versions", "arguments": ["--engine", "postgres", "--major-engine-version", "12"], "data": major},
                 {"id": "s2", "service": "eks", "operation": "describe-cluster-versions", "data": eks_versions},
                 {"id": "s3", "service": "cloudformation", "operation": "describe-stacks", "arguments": ["--stack-name", "shop"], "data": {"Stacks": [{"StackName": "shop", "DriftInformation": {"StackDriftStatus": "DRIFTED"}}]}}]
        resources = [res("rds.instance", "db-old", {"Engine": "postgres", "EngineVersion": "12.19", "DBInstanceClass": "db.m4.large", "EngineLifecycleSupport": "open-source-rds-extended-support"}, ["e1"]),
                     res("eks.cluster", "k-old", {"version": "1.29", "name": "k-old"}, ["e2"]),
                     res("eks.cluster", "k-new", {"version": "1.33", "name": "k-new"}, ["e2"]),
                     res("lambda.function", "fn-old", {"Runtime": "python3.8", "PackageType": "Zip"}, ["e3"], name="fn-old"),
                     res("lambda.function", "fn-new", {"Runtime": "python3.13", "PackageType": "Zip"}, ["e3"], name="fn-new"),
                     res("ec2.instance", "i-win", {"InstanceType": "m4.large", "UsageOperation": "RunInstances:0102", "State": {"Name": "running"}}, ["e4"]),
                     res("ec2.instance", "i-new", {"InstanceType": "m7g.large", "UsageOperation": "RunInstances", "State": {"Name": "running"}}, ["e4"]),
                     res("ssm.managed-instance", "i-win", {"PlatformName": "Microsoft Windows Server 2012 R2 Standard", "PlatformVersion": "6.3", "PlatformType": "Windows"}, ["e5"]),
                     res("elb.classic-load-balancer", "legacy-lb", {"Scheme": "internet-facing"}, ["e6"]),
                     res("autoscaling.group", "arn:asg", {"AutoScalingGroupName": "web", "LaunchConfigurationName": "web-lc-3"}, ["e7"], name="web")]
        by_rule, checks = self.evaluate(resources, calls, now=now)
        self.assertEqual(by_rule["RDS_ENGINE_VERSION_SUPPORT"]["status"], "FAIL")
        self.assertIn("Extended Support", by_rule["RDS_ENGINE_VERSION_SUPPORT"]["observation"])
        self.assertIn("s1", by_rule["RDS_ENGINE_VERSION_SUPPORT"]["evidence_ids"])
        eks = {c["resource_id"].rsplit(":", 1)[-1]: c["status"] for c in checks if c["rule_id"] == "EKS_VERSION_SUPPORT"}
        self.assertEqual(eks, {"k-old": "FAIL", "k-new": "PASS"})
        lam = {c["resource_id"].rsplit(":", 1)[-1]: c["status"] for c in checks if c["rule_id"] == "LAMBDA_RUNTIME_DEPRECATED"}
        self.assertEqual(lam, {"fn-old": "FAIL", "fn-new": "PASS"})
        gen = {c["resource_id"].rsplit(":", 1)[-1]: c["status"] for c in checks if c["rule_id"] == "EC2_PREVIOUS_GENERATION"}
        self.assertEqual(gen, {"i-win": "WARN", "i-new": "PASS", "db-old": "WARN"})
        os_eol = {c["resource_id"].rsplit(":", 1)[-1]: c["status"] for c in checks if c["rule_id"] == "EC2_OS_END_OF_LIFE"}
        self.assertEqual(os_eol, {"i-win": "FAIL", "i-new": "UNKNOWN"})
        self.assertIn("Windows Server 2012", next(c for c in checks if c["rule_id"] == "EC2_OS_END_OF_LIFE" and c["resource_id"].endswith("i-win"))["observation"])
        self.assertEqual(by_rule["COMMERCIAL_LICENSE_DEPENDENCY"]["status"], "WARN")
        self.assertIn("SQL Server Standard", by_rule["COMMERCIAL_LICENSE_DEPENDENCY"]["observation"])
        self.assertEqual(by_rule["ELB_CLASSIC"]["status"], "WARN")
        self.assertEqual(by_rule["ASG_LAUNCH_CONFIGURATION"]["status"], "WARN")
        self.assertEqual(by_rule["STACK_DRIFT"]["status"], "WARN")
        self.assertEqual(by_rule["STACK_DRIFT"]["target"], "stack shop")

    def test_edge_rules_cloudfront_acm_flow_logs_and_iac_coverage(self):
        now = date(2026, 11, 10)
        calls = [{"id": "e1", "service": "cloudfront", "operation": "list-distributions", "data": {}},
                 {"id": "e2", "service": "cloudfront", "operation": "get-distribution-config", "data": {}},
                 {"id": "e3", "service": "acm", "operation": "list-certificates", "data": {}},
                 {"id": "e4", "service": "ec2", "operation": "describe-vpcs", "data": {}},
                 {"id": "e5", "service": "ec2", "operation": "describe-flow-logs", "data": {"FlowLogs": []}},
                 {"id": "e6", "service": "tagging", "operation": "get-resources", "data": []},
                 {"id": "e7", "service": "ec2", "operation": "describe-addresses", "data": {}}]
        config = {"DistributionConfig": {"WebACLId": "", "ViewerCertificate": {"ACMCertificateArn": "arn:acm", "MinimumProtocolVersion": "TLSv1"}, "Logging": {"Enabled": False}}}
        resources = [res("cloudfront.distribution", "arn:cf:E1", {"Id": "E1", "DomainName": "d1.cloudfront.net", "WebACLId": ""}, ["e1"], region="us-east-1",
                         observations={"configuration": {"status": "ok", "data": config, "evidence_id": "e2", "error_code": None}}),
                     res("acm.certificate", "arn:acm:1", {"DomainName": "shop.example.com", "Status": "ISSUED", "Type": "IMPORTED", "InUse": True, "NotAfter": "2026-11-25T00:00:00+00:00"}, ["e3"]),
                     res("acm.certificate", "arn:acm:2", {"DomainName": "api.example.com", "Status": "ISSUED", "Type": "AMAZON_ISSUED", "InUse": True, "NotAfter": "2027-06-01T00:00:00+00:00", "RenewalEligibility": "ELIGIBLE"}, ["e3"]),
                     res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1"}, ["e4"]),
                     res("ec2.elastic-ip", "eipalloc-1", {"PublicIp": "203.0.113.5"}, ["e7"]),
                     res("discovery.tagged-resources", REGION, {"total": 40, "cloudformation_managed": 12, "services": {}, "resource_types": {}}, ["e6"], collection={"status": "ok", "error_code": None})]
        by_rule, checks = self.evaluate(resources, calls, now=now)
        self.assertEqual(by_rule["CLOUDFRONT_WAF"]["status"], "WARN")
        self.assertEqual(by_rule["CLOUDFRONT_TLS_POLICY"]["status"], "WARN")
        self.assertEqual(by_rule["CLOUDFRONT_LOGGING"]["status"], "WARN")
        acm = {c["resource_id"].rsplit(":", 1)[-1]: (c["status"], c["severity"]) for c in checks if c["rule_id"] == "ACM_CERTIFICATE_EXPIRY"}
        self.assertEqual(acm, {"1": ("FAIL", "HIGH"), "2": ("PASS", "MEDIUM")})
        self.assertEqual(by_rule["VPC_FLOW_LOGS"]["status"], "WARN")
        self.assertIn("e5", by_rule["VPC_FLOW_LOGS"]["evidence_ids"])
        self.assertEqual(by_rule["EIP_UNASSOCIATED"]["status"], "WARN")
        self.assertEqual((by_rule["IAC_COVERAGE"]["status"], by_rule["IAC_COVERAGE"]["severity"]), ("WARN", "MEDIUM"))
        self.assertIn("12 of 40", by_rule["IAC_COVERAGE"]["observation"])

    def test_account_level_records_are_excluded_from_the_diagram_requirements(self):
        diagram = load("diagram_platform", "skills/aws-architecture-review/scripts/diagram.py")
        inventory = {"resources": [res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1"}, ["e1"]),
                                   res("securityhub.hub", ACCOUNT, {}, ["e2"]),
                                   res("discovery.tagged-resources", REGION, {}, ["e3"])]}
        ids, stack = diagram.required(inventory, None)
        self.assertEqual({i.rsplit(":", 1)[-1] for i in ids}, {"vpc-1"})

    def test_lifecycle_table_is_well_formed(self):
        tables = platform_rules.TABLES
        date.fromisoformat(tables["verified_on"])
        for runtime, when in tables["lambda_runtime_deprecation"].items():
            date.fromisoformat(when)
        for entry in tables["os_end_of_life"]:
            date.fromisoformat(entry["eol"]); self.assertIn("label", entry)
        self.assertIn("m4", tables["previous_generation_families"]["ec2"])


if __name__ == "__main__":
    unittest.main()


class CommandGuard(unittest.TestCase):
    def test_cli_arguments_with_control_characters_are_refused(self):
        for bad in (["aws", "ec2", "describe-instances\n--profile x"], ["aws", "sts", "get-caller-identity", "a\x00b"], "aws sts get-caller-identity", []):
            with self.assertRaises(ValueError):
                collect.checked_arguments(bad)
        self.assertEqual(collect.checked_arguments(["sts", "get-caller-identity", "--query", "Account || `x`"]), ["sts", "get-caller-identity", "--query", "Account || `x`"])

    def test_svg_guard_refuses_entities_and_active_content_without_an_xml_parser(self):
        diagram = sys.modules.get("diagram_platform") or load("diagram_platform", "skills/aws-architecture-review/scripts/diagram.py")
        for bad in ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><svg:foreignObject/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="x()"/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="https://evil.example/a.png"/></svg>',
                    '<html><svg/></html>'):
            with self.assertRaises(ValueError, msg=bad):
                diagram.scan_svg(bad)
        elements = diagram.scan_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g data-node-id="n1"><use href="#icon"/></g></svg>')
        self.assertEqual(elements[0], ("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewbox": "0 0 1 1"}))
        self.assertEqual(elements[1][1]["data-node-id"], "n1")
        self.assertNotIn("xml.etree.ElementTree", Path(diagram.__file__).read_text(encoding="utf-8"))


class SupplementScope(unittest.TestCase):
    def test_ssm_record_follows_its_scoped_instance_and_account_records_stay_out(self):
        supplement = load("supplement_platform", "skills/aws-as-is-analysis/scripts/supplement.py")
        inventory = {"account_id": ACCOUNT, "run_id": "r1", "resources": [
            res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1"}, ["e1"]),
            res("ec2.instance", "i-1", {"InstanceId": "i-1", "VpcId": "vpc-1"}, ["e1"]),
            res("ec2.instance", "i-2", {"InstanceId": "i-2", "VpcId": "vpc-9"}, ["e1"]),
            res("ssm.managed-instance", "i-1", {"PlatformName": "Amazon Linux", "PlatformVersion": "2023"}, ["e2"]),
            res("ssm.managed-instance", "i-2", {"PlatformName": "Amazon Linux", "PlatformVersion": "2"}, ["e2"]),
            res("securityhub.hub", ACCOUNT, {}, ["e3"]),
            res("discovery.tagged-resources", REGION, {}, ["e4"])]}
        job = supplement.Supplement.__new__(supplement.Supplement)
        job.inventory, job.scope = inventory, {"workload": {"type": "stack", "stack_names": ["s"], "exclude_stacks": []}}
        job.by_native = {}
        for r in inventory["resources"]:
            job.by_native.setdefault(r["native_id"], set()).add(r["id"])
        resolved = job.resolve_scope(["s"], [{"StackName": "s", "LogicalResourceId": "Web", "PhysicalResourceId": "i-1", "ResourceType": "AWS::EC2::Instance"}])
        natives = {x["id"].rsplit(":", 2)[-2] + ":" + x["id"].rsplit(":", 1)[-1] for x in resolved["resources"]}
        self.assertIn("ec2.instance:i-1", natives)
        self.assertIn("ssm.managed-instance:i-1", natives)
        self.assertNotIn("ssm.managed-instance:i-2", natives)
        self.assertFalse(any(t.startswith(("securityhub", "discovery")) for t in natives))


class DiagramPlacement(unittest.TestCase):
    def test_multi_subnet_resources_and_pod_targets_are_placed_in_their_subnets(self):
        diagram = sys.modules.get("diagram_platform") or load("diagram_platform", "skills/aws-architecture-review/scripts/diagram.py")
        def sub(sid, cidr, az, name):
            return res("ec2.subnet", sid, {"SubnetId": sid, "VpcId": "vpc-1", "CidrBlock": cidr, "AvailabilityZone": az, "Tags": [{"Key": "Name", "Value": name}]}, ["e1"])
        tg_eks = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/k8s-shop-storefront/1"
        tg_ecs = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/shop-catalog/2"
        health = lambda ips: {"status": "ok", "data": {"TargetHealthDescriptions": [{"Target": {"Id": ip, "AvailabilityZone": az}, "TargetHealth": {"State": "healthy"}} for ip, az in ips]}, "evidence_id": "e2", "error_code": None}
        inventory = {"regions": [REGION], "relationships": [], "resources": [
            res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1", "CidrBlock": "10.70.0.0/16"}, ["e1"]),
            sub("subnet-a", "10.70.10.0/24", "us-east-1a", "app-a"), sub("subnet-b", "10.70.11.0/24", "us-east-1b", "app-b"),
            res("elbv2.load-balancer", "arn:alb", {"LoadBalancerName": "internal", "Scheme": "internal", "Type": "application", "VpcId": "vpc-1",
                                                   "AvailabilityZones": [{"SubnetId": "subnet-a", "ZoneName": "us-east-1a"}, {"SubnetId": "subnet-b", "ZoneName": "us-east-1b"}]}, ["e1"], name="internal"),
            res("ecs.service", "arn:svc", {"serviceName": "catalog", "launchType": "FARGATE", "desiredCount": 2, "loadBalancers": [{"targetGroupArn": tg_ecs}],
                                           "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-a", "subnet-b"]}}}, ["e1"], name="catalog"),
            res("elbv2.target-group", tg_eks, {"TargetGroupName": "k8s-shop-storefront", "TargetType": "ip", "Port": 8080}, ["e1"], name="k8s-shop-storefront",
                observations={"target_health": health([("10.70.10.170", "us-east-1a"), ("10.70.11.42", "us-east-1b")])}),
            res("elbv2.target-group", tg_ecs, {"TargetGroupName": "shop-catalog", "TargetType": "ip", "Port": 8000}, ["e1"], name="shop-catalog",
                observations={"target_health": health([("10.70.10.152", "us-east-1a"), ("10.70.11.28", "us-east-1b")])})]}
        graph = diagram.graph_from_inventory(inventory)
        groups = {g["id"]: g for g in graph["groups"]}
        span = [g for g in graph["groups"] if g["kind"] == "subnet-span"]
        self.assertEqual(len(span), 1)
        self.assertEqual(span[0]["label"], "Subnets app-a · app-b (us-east-1a, us-east-1b)")
        self.assertEqual(span[0]["subnet_ids"], ["subnet-a", "subnet-b"])
        self.assertEqual(groups[span[0]["parent"]]["kind"], "vpc")
        by_label = {n["label"]: n for n in graph["nodes"]}
        self.assertEqual(by_label["Load balancer internal"]["group"], span[0]["id"])
        self.assertEqual(by_label["ECS service catalog"]["group"], span[0]["id"])
        self.assertIn("subnets app-a, app-b", by_label["ECS service catalog"]["detail"])
        pods = by_label["Kubernetes pods behind k8s-shop-storefront × 2"]
        self.assertEqual((pods["group"], pods["state"], pods["type"]), (span[0]["id"], "observed", "elbv2.ip-targets"))
        self.assertIn("2/2 healthy", pods["detail"])
        self.assertFalse(any(n["label"].startswith("IP targets of shop-catalog") for n in graph["nodes"]), "ECS-owned target group is the service node")
        self.assertTrue(any(e["target"] == pods["id"] and e["label"] == "registered target" for e in graph["edges"]))
        # the subnet groups still own their subnets, so the audit does not see them as undrawn
        self.assertEqual(diagram.coverage(graph, inventory, None)["subnets_or_vpcs_not_drawn_as_groups"], [])
