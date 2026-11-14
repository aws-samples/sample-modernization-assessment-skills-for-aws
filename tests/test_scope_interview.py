"""Scope interview contract: discovery output shape, scope.json validation, and the skill text that drives it.
No AWS calls: discovery is exercised with a fake `aws` executable on PATH."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from test_workflow import ROOT, load
from harness import run_python

scope = load("scope", "skills/aws-cli-setup/scripts/scope.py")
DISCOVERY = ROOT / "skills/aws-cli-setup/scripts/scope_discovery.py"
SCOPE = ROOT / "skills/aws-cli-setup/scripts/scope.py"

FAKE_AWS = r'''#!/usr/bin/env python3
"""Fake `aws` CLI: answers the read-only calls scope_discovery.py makes and fails on anything else."""
import json, sys
a = sys.argv[1:]
def arg(flag):
    return a[a.index(flag) + 1] if flag in a else None
svc, op = a[0], a[1]
region = arg("--region"); query = arg("--query")
if any(x in a for x in ("delete", "create", "update", "put")):
    sys.exit(99)
if (svc, op) == ("sts", "get-caller-identity"):
    print(json.dumps({"Account": "111111111111", "Arn": "arn:aws:sts::111111111111:assumed-role/Fixture/x", "UserId": "AROAFIXTURE:x"})); sys.exit(0)
if svc == "resourcegroupstaggingapi":
    if region == "eu-west-1":
        sys.stderr.write("An error occurred (AccessDeniedException) when calling the GetResources operation: denied\n"); sys.exit(254)
    items = {"us-east-1": 5, "ap-northeast-2": 0}.get(region, 0)
    if query == "length(ResourceTagMappingList)":
        print(json.dumps(items)); sys.exit(0)
    tags = [{"Key": "Environment", "Value": "prod"}] * 3 + [{"Key": "Project", "Value": "shop"}] * 2 + [{"Key": "aws:cloudformation:stack-name", "Value": "shop"}]
    print(json.dumps(tags if items else [])); sys.exit(0)
if (svc, op) == ("cloudformation", "list-stacks"):
    print(json.dumps([{"name": "shop", "status": "CREATE_COMPLETE", "parent": None}, {"name": "shop-nested", "status": "CREATE_COMPLETE", "parent": "arn:x"}])); sys.exit(0)
if (svc, op) == ("cloudformation", "list-stack-resources"):
    print(json.dumps(["AWS::EC2::Instance", "AWS::EC2::Instance", "AWS::ElasticLoadBalancingV2::LoadBalancer"])); sys.exit(0)
if (svc, op) == ("ssm", "describe-instance-information"):
    print(json.dumps([{"id": "i-0123456789abcdef0", "ping": "Online", "platform": "Amazon Linux"}])); sys.exit(0)
sys.stderr.write(f"unexpected call {svc} {op}\n"); sys.exit(97)
'''


def with_fake_aws(temporary):
    bindir = Path(temporary) / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    env.pop("AWS_PROFILE", None)
    return env


class ScopeDiscoveryTests(unittest.TestCase):
    def test_discovery_is_read_only_and_offers_real_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = with_fake_aws(temporary)
            out = Path(temporary) / "scope-options.json"
            result = run_python(str(DISCOVERY), "--profile", "fixture", "--expected-account", "111111111111",
                                     "--regions", "us-east-1,ap-northeast-2,eu-west-1", "--output", str(out),
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(out.read_text())
            self.assertTrue(data["read_only"])
            self.assertEqual(data["identity"]["account_id"], "111111111111")
            self.assertEqual(data["regions"]["us-east-1"], {"status": "ok", "resources": 5})
            self.assertEqual(data["regions"]["ap-northeast-2"]["resources"], 0)
            self.assertEqual(data["regions"]["eu-west-1"]["status"], "access_denied")
            # Only regions that hold resources are explored further; nested stacks are folded into their parent.
            self.assertEqual(list(data["stacks"]), ["us-east-1"])
            self.assertEqual([s["name"] for s in data["stacks"]["us-east-1"]["items"]], ["shop"])
            self.assertEqual(data["stacks"]["us-east-1"]["items"][0]["resource_count"], 3)
            keys = [k["key"] for k in data["tags"]["us-east-1"]["keys"]]
            self.assertEqual(keys[:2], ["Environment", "Project"])  # aws:* keys dropped, conventional keys first
            self.assertEqual(data["ssm_managed_instances"]["us-east-1"]["online"], 1)
            self.assertEqual(data["proposed_defaults"]["regions"], ["us-east-1"])
            self.assertEqual(data["proposed_defaults"]["workload"]["type"], "stack")
            self.assertEqual(data["proposed_defaults"]["workload"]["stack_name"], "shop")
            self.assertEqual(len(data["denied_or_failed"]), 1)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["active_regions"], ["us-east-1"])

    def test_discovery_stops_on_account_mismatch_and_refuses_non_read_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = with_fake_aws(temporary)
            out = Path(temporary) / "x.json"
            result = run_python(str(DISCOVERY), "--profile", "fixture", "--expected-account", "222222222222",
                                     "--regions", "us-east-1", "--output", str(out), capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("account_mismatch", result.stderr)
            self.assertFalse(out.exists())
        discovery = load("scope_discovery", "skills/aws-cli-setup/scripts/scope_discovery.py")
        class Args: profile = None; timeout = 5
        d = discovery.Discovery.__new__(discovery.Discovery)
        d.args = Args(); d.env = {}; d.aws = "aws"; d.calls = []
        with self.assertRaises(ValueError):
            d.call("cloudformation", "detect-stack-drift", "us-east-1")


class ScopeFileTests(unittest.TestCase):
    def test_scope_write_validate_and_rejections(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "scope.json"
            result = run_python(str(SCOPE), "write", "--account", "111111111111", "--regions", "us-east-1,ap-northeast-2",
                                     "--workload-name", "shop", "--type", "stack", "--stack", "shop", "--exclude-stack", "CDKToolkit",
                                     "--inspect-instance", "i-0123456789abcdef0", "--language", "ko", "--profile", "assessment",
                                     "--confirmed-by", "interview", "--output", str(out), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(out.read_text())
            self.assertEqual(doc["workload"], {"type": "stack", "exclude_stacks": ["CDKToolkit"], "stack_names": ["shop"]})
            self.assertEqual(doc["host_inspection"], {"enabled": True, "instance_ids": ["i-0123456789abcdef0"]})
            self.assertEqual(doc["auth"], {"mode": "profile", "profile": "assessment"})
            self.assertEqual(doc["services"][:3], ["ec2", "elb", "elbv2"])
            self.assertEqual(scope.validate(doc), [])
            check = run_python(str(SCOPE), "validate", str(out), capture_output=True, text=True)
            self.assertEqual(check.returncode, 0)
            self.assertEqual(doc["context"], {"environment": "unknown", "operator": "unknown", "support_plan": "unknown", "compliance": ["unknown"]}, "context defaults keep 'confirm' working")
            ctx = Path(temporary) / "scope-ctx.json"
            run_python(str(SCOPE), "write", "--account", "111111111111", "--regions", "us-east-1", "--workload-name", "shop", "--type", "account", "--language", "ko",
                       "--profile", "p", "--confirmed-by", "interview", "--environment-type", "production", "--operator", "provider", "--support-plan", "business", "--compliance", "ISMS-P, PCI DSS", "--output", str(ctx),
                       capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(ctx.read_text())["context"], {"environment": "production", "operator": "provider", "support_plan": "business", "compliance": ["ISMS-P", "PCI DSS"]})
            bad = json.loads(ctx.read_text()); bad["context"]["support_plan"] = "platinum"; ctx.write_text(json.dumps(bad))
            self.assertNotEqual(run_python(str(SCOPE), "validate", str(ctx), capture_output=True, text=True).returncode, 0)
            # Refuses to overwrite.
            again = run_python(str(SCOPE), "write", "--account", "111111111111", "--regions", "us-east-1",
                                    "--workload-name", "shop", "--type", "account", "--language", "en", "--environment",
                                    "--confirmed-by", "defaults", "--output", str(out), capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
        bad = json.loads(json.dumps(doc))
        bad["workload"] = {"type": "tag", "tags": {}}
        bad["auth"] = {"mode": "profile", "profile": "x", "aws_secret_access_key": "nope"}
        bad["host_inspection"] = {"enabled": True, "instance_ids": ["not-an-id"]}
        problems = scope.validate(bad)
        self.assertTrue(any("tags" in p for p in problems))
        self.assertTrue(any("credential" in p for p in problems))
        self.assertTrue(any("instance_ids" in p for p in problems))

    def test_skill_texts_carry_the_interview_contract(self):
        setup = (ROOT / "skills/aws-cli-setup/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("start the assessment", setup.split("---")[1])
        self.assertIn("scope_discovery.py", setup)
        self.assertIn("scope.py", setup)
        self.assertIn("references/scope-interview.md", setup)
        reference = (ROOT / "skills/aws-cli-setup/references/scope-interview.md").read_text(encoding="utf-8")
        for needle in ("AskUserQuestion", "Kiro CLI, Codex", "confirm", "defaults", "Host inspection", "Report language"):
            self.assertIn(needle, reference)
        for skill in ("aws-mcp-setup", "aws-as-is-analysis", "aws-architecture-review", "aws-modernization-pathway"):
            self.assertIn("scope.json", (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"), skill)


if __name__ == "__main__":
    unittest.main()
