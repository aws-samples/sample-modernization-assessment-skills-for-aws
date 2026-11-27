"""Report quality guards: workload scope, supplementary checks, diagram coverage, IaC changes, references,
open questions, the scanner-neutral security chapter and localization labels. No AWS calls."""
import argparse
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


sys.path.insert(0, str(ROOT / "skills/aws-architecture-review/scripts"))
review = load("rq_review", "skills/aws-architecture-review/scripts/review.py")
diagram = load("rq_diagram", "skills/aws-architecture-review/scripts/diagram.py")
supplement = load("rq_supplement", "skills/aws-as-is-analysis/scripts/supplement.py")
localize = load("rq_localize", "skills/aws-mcp-setup/scripts/localize_artifacts.py")
REPORT = ROOT / "skills/aws-modernization-pathway/scripts/assessment_report.py"
RECOMMEND = ROOT / "skills/aws-modernization-pathway/scripts/recommend.py"

ACCOUNT, REGION = "111111111111", "us-east-1"
FAKE_KEY_ID = "AKIA" + "Z" * 16  # synthetic access-key shape for the redaction test; built at runtime
VPC, SUB_A, SUB_B = "vpc-1", "subnet-a", "subnet-b"
ALB_STACK = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/web/1"
ALB_K8S = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/k8s-ingress/2"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts"
TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Resources:
  Vpc:
    Type: AWS::EC2::VPC
  Db:
    Type: AWS::RDS::DBCluster
    Properties:
      BackupRetentionPeriod: 1
      DeletionProtection: false
  Cache:
    Type: AWS::ElastiCache::ReplicationGroup
Outputs:
  X:
    Value: y
"""


def rid(kind, native):
    return f"{ACCOUNT}:{REGION}:{kind}:{native}"


def resource(kind, native, attributes, evidence="e0001", observations=None, name=None):
    return {"id": rid(kind, native), "type": kind, "native_id": native, "name": name or native, "account_id": ACCOUNT,
            "region": REGION, "attributes": attributes, "evidence_ids": [evidence], "observations": observations or {}}


def listeners(protocol):
    return {"listeners": {"status": "ok", "data": {"Listeners": [{"ListenerArn": "l", "Protocol": protocol, "Port": 80,
                                                                 "DefaultActions": [{"Type": "forward"}]}]}, "evidence_id": "e0001"}}


def workload_snapshot(root):
    """Inventory with a VPC in two AZs, a stack ALB, a controller-created ALB, an alarm on an unsubscribed topic,
    plus supplementary evidence (template, stack resources, IAM policy, subscriptions)."""
    (root / "raw/supplementary").mkdir(parents=True)
    resources = [
        resource("ec2.vpc", VPC, {"VpcId": VPC, "CidrBlock": "10.0.0.0/16"}),
        resource("ec2.subnet", SUB_A, {"VpcId": VPC, "SubnetId": SUB_A, "AvailabilityZone": "us-east-1a", "CidrBlock": "10.0.0.0/24"}),
        resource("ec2.subnet", SUB_B, {"VpcId": VPC, "SubnetId": SUB_B, "AvailabilityZone": "us-east-1b", "CidrBlock": "10.0.1.0/24"}),
        resource("elbv2.load-balancer", ALB_STACK, {"Type": "application", "Scheme": "internal", "LoadBalancerName": "web", "VpcId": VPC,
                 "AvailabilityZones": [{"ZoneName": "us-east-1a", "SubnetId": SUB_A}, {"ZoneName": "us-east-1b", "SubnetId": SUB_B}]},
                 observations=listeners("HTTPS"), name="web"),
        resource("elbv2.load-balancer", ALB_K8S, {"Type": "application", "Scheme": "internal", "LoadBalancerName": "k8s-ingress", "VpcId": VPC,
                 "AvailabilityZones": [{"ZoneName": "us-east-1a", "SubnetId": SUB_A}, {"ZoneName": "us-east-1b", "SubnetId": SUB_B}]},
                 observations=listeners("HTTP"), name="k8s-ingress"),
        resource("ec2.instance", "i-1", {"VpcId": VPC, "SubnetId": SUB_A, "InstanceType": "t3.large"}),
        resource("ec2.security-group", "sg-1", {"VpcId": VPC, "GroupId": "sg-1", "IpPermissions": []}),
        resource("cloudwatch.alarm", "cpu", {"AlarmName": "cpu", "AlarmActions": [TOPIC]}),
        resource("sns.topic", TOPIC, {"TopicArn": TOPIC}),
        resource("ec2.vpc", "vpc-other", {"VpcId": "vpc-other"}),
        resource("ec2.instance", "i-other", {"VpcId": "vpc-other", "SubnetId": "subnet-x", "InstanceType": "m5.large"}),
    ]
    raw0 = root / "raw/e0001.json"
    raw0.write_text("{}")
    calls = [{"id": "e0001", "service": "ec2", "operation": "describe-vpcs", "region": REGION, "status": "ok",
              "raw_path": "raw/e0001.json", "sha256": hashlib.sha256(raw0.read_bytes()).hexdigest()}]

    def add(eid, service, operation, args, payload, resource_ids=(), origin="workload"):
        path = root / f"raw/supplementary/{eid}.json"
        path.write_text(json.dumps(payload))
        calls.append({"id": eid, "service": service, "operation": operation, "region": REGION, "arguments": args, "status": "ok",
                      "raw_path": f"raw/supplementary/{eid}.json", "raw_format": "json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "resource_ids": list(resource_ids), "provenance": {"origin": origin}})
    add("s0001", "cloudformation", "get-template", ["--stack-name", "app", "--template-stage", "Original"], {"TemplateBody": TEMPLATE})
    add("s0002", "iam", "get-role-policy", ["--role-name", "AppRole", "--policy-name", "app"],
        {"PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}}, [rid("ec2.instance", "i-1")], origin="role/AppRole")
    add("s0003", "sns", "list-subscriptions-by-topic", ["--topic-arn", TOPIC], {"Subscriptions": []}, [rid("sns.topic", TOPIC)])
    common = {"schema_version": "1.0", "run_id": "rq", "account_id": ACCOUNT, "regions": [REGION], "services": ["ec2", "elbv2"]}
    inventory = dict(common, collection_status="complete", resources=resources, relationships=[])
    manifest = dict(common, status="complete", completed_at="2026-11-06T00:00:00Z", truncations=[], coverage={"ok": len(calls)}, calls=calls)
    (root / "inventory.json").write_text(json.dumps(inventory))
    (root / "collection-manifest.json").write_text(json.dumps(manifest))
    scope = {"schema_version": "1.0", "account_id": ACCOUNT, "run_id": "rq",
             "resources": [{"id": r["id"], "reasons": ["test"]} for r in resources if "other" not in r["native_id"]],
             "vpcs": [VPC], "stack_resources": [
                 {"StackName": "app", "LogicalResourceId": "Vpc", "PhysicalResourceId": VPC, "ResourceType": "AWS::EC2::VPC"},
                 {"StackName": "app", "LogicalResourceId": "Cache", "PhysicalResourceId": "cache-1", "ResourceType": "AWS::ElastiCache::ReplicationGroup"}]}
    (root / "scope-resources.json").write_text(json.dumps(scope))
    return inventory, manifest, scope


class WorkloadScopeAndChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snap"
        self.inventory, self.manifest, self.scope = workload_snapshot(self.root)
        _, _, self.calls = review.verify_inputs(self.root)
        self.scoped = review.load_scope(self.root / "scope-resources.json", self.inventory)
        self.checks = review.evaluate(self.inventory, self.manifest, self.calls, self.root, self.scoped)

    def tearDown(self):
        self.tmp.cleanup()

    def by_rule(self, rule):
        return [c for c in self.checks if c["rule_id"] == rule]

    def test_out_of_scope_resources_get_no_checks(self):
        self.assertFalse([c for c in self.checks if c.get("resource_id") and "other" in c["resource_id"]])

    def test_tls_check_covers_load_balancers_outside_cloudformation(self):
        tls = {c["resource_id"]: c["status"] for c in self.by_rule("ELB_TLS_LISTENER")}
        self.assertEqual(tls[rid("elbv2.load-balancer", ALB_K8S)], "WARN")
        self.assertEqual(tls[rid("elbv2.load-balancer", ALB_STACK)], "PASS")

    def test_iam_wildcard_and_unsubscribed_alarm_topic(self):
        iam = self.by_rule("IAM_WILDCARD_PERMISSION")
        self.assertEqual((iam[0]["status"], iam[0]["severity"]), ("FAIL", "HIGH"))
        self.assertIn("AppRole", iam[0]["target"])
        alarm = self.by_rule("ALARM_NOTIFICATION_TARGET")[0]
        self.assertEqual(alarm["status"], "FAIL")
        self.assertIn("0 subscription", alarm["observation"])

    def test_alarm_coverage_matches_namespace_and_dimension(self):
        # The fixture alarm has no dimensions, so every alarm-worthy resource is uncovered.
        coverage = {c["resource_id"]: c["status"] for c in self.by_rule("ALARM_COVERAGE")}
        self.assertEqual(coverage[rid("elbv2.load-balancer", ALB_STACK)], "WARN")
        self.assertEqual(coverage[rid("elbv2.load-balancer", ALB_K8S)], "WARN", "controller-created ALB is judged too")
        self.assertEqual(coverage[rid("ec2.instance", "i-1")], "WARN")
        self.assertNotIn(rid("ec2.instance", "i-other"), coverage, "out of scope")

    def test_unknown_checks_carry_owner_questions(self):
        questions = [c for c in self.checks if c["rule_id"] in {"OPS_PROCEDURES", "REL_RESTORE_TEST"}]
        self.assertTrue(all(c.get("question", "").endswith("?") for c in questions))

    def test_judged_finding_without_target_is_rejected(self):
        bad = dict(self.by_rule("IAM_WILDCARD_PERMISSION")[0], id="m0001", resource_id=None, target=None)
        with self.assertRaisesRegex(ValueError, "resource_id or target"):
            review.score([bad], self.calls, {r["id"] for r in self.inventory["resources"]})

    def test_manual_override_needs_reason_and_suppress_drops_baseline(self):
        base = self.by_rule("ELB_TLS_LISTENER")[0]
        changed = dict(base, id="m0001", status="FAIL")
        with self.assertRaisesRegex(ValueError, "assessment_reason"):
            review.merge_manual(self.checks, [changed])
        merged = review.merge_manual(self.checks, [dict(changed, assessment_reason="order traffic is sensitive")])
        self.assertIn("m0001", {c["id"] for c in merged})
        self.assertNotIn(base["id"], {c["id"] for c in merged})
        dropped = review.merge_manual(self.checks, [{"id": base["id"], "suppress": True, "assessment_reason": "test fixture"}])
        self.assertNotIn(base["id"], {c["id"] for c in dropped})

    def test_iac_changes_are_checked_against_the_collected_template(self):
        check = dict(self.by_rule("ELB_TLS_LISTENER")[0], evidence_ids=["e0001"])
        good = {"stack": "app", "logical_id": "Db", "property": "Properties.BackupRetentionPeriod", "current": "1", "proposed": "7", "evidence_id": "s0001"}
        review.validate_iac_changes([dict(check, iac_changes=[good])], self.root, self.calls)
        for broken, message in ((dict(good, logical_id="Nope"), "not a resource"), (dict(good, current="35"), "not found"),
                                (dict(good, current=None), "is set"), (dict(good, evidence_id="s0002"), "get-template"),
                                (dict(good, stack="other"), "another stack")):
            with self.assertRaisesRegex(ValueError, message):
                review.validate_iac_changes([dict(check, evidence_ids=["e0001"], iac_changes=[broken])], self.root, self.calls)
        review.validate_iac_changes([dict(check, evidence_ids=["e0001"], iac_changes=[dict(good, property="Properties.MultiAZ", current=None)])], self.root, self.calls)


class DiagramCoverage(unittest.TestCase):
    def test_default_graph_draws_vpc_az_subnets_and_every_scoped_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory, _, scope = workload_snapshot(Path(tmp) / "snap")
            graph = diagram.graph_from_inventory(inventory, scope)
            kinds = {g["kind"] for g in graph["groups"]}
            self.assertTrue({"region", "vpc", "az", "subnet"} <= kinds)
            self.assertEqual({g["id"] for g in graph["groups"] if g["kind"] == "subnet"}, {SUB_A, SUB_B})
            instance = next(n for n in graph["nodes"] if rid("ec2.instance", "i-1") in n["resource_ids"])
            self.assertEqual(instance["group"], SUB_A, "an instance sits in its subnet")
            self.assertEqual(diagram.coverage(graph, inventory, scope)["status"], "pass")
            self.assertIn("app/Cache", {k for n in graph["nodes"] for k in n.get("stack_resources", [])})
            self.assertNotIn(rid("ec2.instance", "i-other"), {r for n in graph["nodes"] for r in n["resource_ids"]})

    def test_audit_fails_when_subnets_or_resources_are_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory, _, scope = workload_snapshot(Path(tmp) / "snap")
            graph = diagram.graph_from_inventory(inventory, scope)
            trimmed = dict(graph, groups=[g for g in graph["groups"] if g["kind"] not in {"subnet", "az"}])
            for node in trimmed["nodes"]:
                if node["group"] not in {g["id"] for g in trimmed["groups"]}:
                    node["group"] = VPC
            result = diagram.coverage(trimmed, inventory, scope)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["missing_by_type"].get("ec2.subnet"), 2)
            self.assertEqual(result["missing_availability_zones"], ["us-east-1a", "us-east-1b"])
            svg = Path(tmp) / "final.svg"
            ids = "".join(f'<g data-node-id="{n["id"]}"/>' for n in graph["nodes"]) + "".join(f'<g data-group-id="{g["id"]}"/>' for g in graph["groups"])
            svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{ids}</svg>')
            self.assertEqual(diagram.audit(graph, svg, inventory, scope)["status"], "pass")
            svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{ids.replace(f"data-group-id=\"{SUB_A}\"", "data-other=\"x\"")}</svg>')
            self.assertIn(SUB_A, diagram.audit(graph, svg, inventory, scope)["missing_groups"])


class SupplementCollector(unittest.TestCase):
    def test_scope_includes_controller_created_alb_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap"
            inventory, _, _ = workload_snapshot(snap)
            scope = {"account_id": ACCOUNT, "workload_name": "app", "workload": {"type": "stack", "stack_names": ["app"]}}
            out = Path(tmp) / "out"
            out.mkdir()
            replies = {
                ("cloudformation", "list-stack-resources"): {"StackResourceSummaries": [
                    {"LogicalResourceId": "Vpc", "PhysicalResourceId": VPC, "ResourceType": "AWS::EC2::VPC"},
                    {"LogicalResourceId": "Web", "PhysicalResourceId": ALB_STACK, "ResourceType": "AWS::ElasticLoadBalancingV2::LoadBalancer"},
                    {"LogicalResourceId": "Secret", "PhysicalResourceId": "arn:aws:secretsmanager:x:secret:db", "ResourceType": "AWS::SecretsManager::Secret"}]},
                ("cloudformation", "describe-stacks"): {"Stacks": [{"StackName": "app", "Outputs": [{"OutputValue": "leak"}],
                                                                    "Parameters": [{"ParameterKey": "Pw", "ParameterValue": "hunter2"}]}]},
                ("cloudformation", "get-template"): {"TemplateBody": f"password: hunter2secret\nkey: {FAKE_KEY_ID}\n"},
                ("secretsmanager", "describe-secret"): {"Name": "db", "RotationEnabled": False},
            }
            commands = []

            def fake(command, **kwargs):
                commands.append(command)
                payload = replies.get((command[1], command[2]), {})
                return completed(0, json.dumps(payload), "")
            args = argparse.Namespace(profile="p", output=out, max_calls=100, max_workers=4, max_stacks=5, max_instances=5,
                                      source_files=0, timeout=10)
            manifest = json.loads((snap / "collection-manifest.json").read_text())
            with patch.object(supplement.shutil, "which", return_value="/fake/aws"), patch.object(supplement.subprocess, "run", side_effect=fake):
                job = supplement.Supplement(args, inventory, manifest, scope)
                _, resolved = job.collect()
            ids = {x["id"]: x["reasons"] for x in resolved["resources"]}
            self.assertIn(rid("elbv2.load-balancer", ALB_K8S), ids, "not in the stack but inside the in-scope VPC")
            self.assertTrue(any(r.startswith("in-scope-vpc") for r in ids[rid("elbv2.load-balancer", ALB_K8S)]))
            self.assertNotIn(rid("ec2.instance", "i-other"), ids)
            text = "".join(p.read_text() for p in out.glob("*.json"))
            for secret in ("hunter2", "leak", FAKE_KEY_ID):
                self.assertNotIn(secret, text)
            operations = {(c[1], c[2]) for c in commands}
            self.assertIn(("elbv2", "describe-load-balancer-attributes"), operations)
            self.assertTrue(all(op.startswith(supplement.READ_VERBS) for _, op in operations))
            with self.assertRaises(ValueError):
                job.plan("secretsmanager", "get-secret-value", "--secret-id", "db")


class ReportGuards(unittest.TestCase):
    def build(self, root):
        snap = root / "03-as-is"
        workload_snapshot(snap)
        r = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"), "--as-is", str(snap), "--output", str(root / "04-review"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_python(str(RECOMMEND), "--inventory", str(snap / "inventory.json"), "--findings", str(root / "04-review/findings.json"),
                            "--workload", "rq", "--output", str(root / "05-modernization"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((root / "04-review/findings.json").read_text())["checks"]

    def test_references_questions_and_diagram_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checks = self.build(root)
            iam = next(c for c in checks if c["rule_id"] == "IAM_WILDCARD_PERMISSION")
            issues = root / "key-issues.json"
            issues.write_text(json.dumps({"issues": [{"title": "Roles can touch every bucket", "problem": "p", "impact": "i", "action": 'Spreading the database over two Availability Zones would keep it serving if one zone fails. If cost is the reason for a single instance, right-size it first with Compute Optimizer, then add the replica.',
                                                      "urgency": "now", "finding_ids": [iam["id"]]}]}))
            svg = root / "as-is.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g data-node-id="n1"/></svg>')
            run = run_python(str(REPORT), "--run-dir", str(root), "--workload", "rq", "--key-issues", str(issues), "--as-is-diagram", str(svg),
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout)["report_status"].startswith("Draft - diagram does not cover"), "no coverage audit, no final")
            html = (root / "06-assessment/assessment.html").read_text()
            details = html  # the appendix is part of the one report file
            self.assertIn("SEC03-BP02 Grant least privilege access", html, "curated Well-Architected reference")
            # Evidence IDs link to a details-page row that names the call behind them; count tiles open their lists.
            self.assertIn('id="ev-s0002"', details)
            self.assertIn('href="#ev-s0002"', details)
            self.assertIn("aws iam get-role-policy", details)
            self.assertIn('class="kpi fail kpi-list"', html)
            self.assertIn(f'href="#{iam["id"]}"', html)
            md = (root / "06-assessment/assessment.md").read_text()
            self.assertIn("### A. Evidence", md)
            self.assertIn("| s0002 | `aws iam get-role-policy`", md)
            self.assertNotIn('id="questions"', html); self.assertNotIn('id="questions"', details)
            self.assertIn("RTO/RPO", details, "open questions stay in their pillar")
            self.assertIn("Evidence table (", details); self.assertIn("f0001 = a finding from an automated check", html)
            self.assertNotIn("Service Screener", html); self.assertNotIn("Service Screener", details)
            md = (root / "06-assessment/assessment.md").read_text()
            self.assertIn("**References**", md)
            issues.write_text(json.dumps({"issues": [{"title": "t", "problem": "p", "impact": "i", "action": 'Spreading the database over two Availability Zones would keep it serving if one zone fails. If cost is the reason for a single instance, right-size it first with Compute Optimizer, then add the replica.', "urgency": "now",
                                                      "finding_ids": [iam["id"]], "references": [{"title": "blog", "url": "https://example.com/x"}]}]}))
            bad = run_python(str(REPORT), "--run-dir", str(root), "--workload", "rq", "--key-issues", str(issues), "--output", str(root / "06-b"),
                                 capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("AWS documentation", bad.stderr)


class ReferenceCatalog(unittest.TestCase):
    def test_catalog_is_consistent_and_copies_match(self):
        a = ROOT / "skills/aws-architecture-review/assets/references.json"
        b = ROOT / "skills/aws-modernization-pathway/assets/references.json"
        self.assertEqual(a.read_bytes(), b.read_bytes())
        data = json.loads(a.read_text())
        for key, ref in data["references"].items():
            self.assertTrue(ref["url"].startswith(("https://docs.aws.amazon.com/", "https://aws.amazon.com/")), key)
            self.assertTrue(ref["title"].strip())
        for rule, keys in data["rules"].items():
            self.assertTrue(keys and all(k in data["references"] for k in keys), rule)


class NextStepsCatalog(unittest.TestCase):
    def setUp(self):
        self.ns = load("rq_next_steps", "skills/aws-modernization-pathway/scripts/next_steps.py")

    def test_catalog_entries_have_conditions_and_official_sources(self):
        cat = self.ns.CATALOG
        for item in cat["services"] + cat["assets"]:
            self.assertTrue(item.get("when"), item["id"])
            self.assertTrue(item["url"].startswith("https://") and any(h in item["url"] for h in ("aws.amazon.com", "github.com/aws-samples", "workshops.aws")), item["id"])
        for s in cat["services"]:
            self.assertTrue(s["official_description"].strip(), s["id"])
            self.assertTrue(any(h in s["source"] for h in ("docs.aws.amazon.com", "aws.amazon.com/about-aws/whats-new", "aws.amazon.com/documentation-overview")), s["id"])
            self.assertRegex(s["source_checked_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_selection_is_conditional_and_pathway_bound(self):
        inventory = {"resources": [
            {"id": "r1", "type": "rds.cluster", "attributes": {"Engine": "aurora-postgresql"}, "name": "db"},
            {"id": "r2", "type": "ec2.instance", "attributes": {"PlatformDetails": "Linux/UNIX"}, "name": "web"}]}
        checks = [{"id": "f1", "rule_id": "AURORA_SINGLE_INSTANCE", "status": "FAIL", "severity": "HIGH"},
                  {"id": "f2", "rule_id": "IAM_WILDCARD_PERMISSION", "status": "FAIL", "severity": "HIGH"}]
        cands = [{"pathway": "managed-database", "status": "conditional", "problem": "single writer", "rationale": "keep and improve the existing Aurora cluster", "finding_ids": ["f1"]},
                 {"pathway": "genai", "status": "conditional", "problem": "chatbot", "rationale": "single Q&A chatbot on Bedrock", "finding_ids": []},
                 {"pathway": "modern-devops", "status": "candidate", "problem": "", "rationale": "", "finding_ids": ["f2"]},
                 {"pathway": "serverless", "status": "not_assessed", "problem": "", "rationale": "", "finding_ids": []}]
        sel = self.ns.select(cands, checks, inventory)
        self.assertNotIn("serverless", sel, "unrecommended pathways get nothing")
        db = {s["id"] for s in sel["managed-database"]["services"]} | {a["id"] for a in sel["managed-database"]["assets"]}
        self.assertIn("aurora", db)
        self.assertNotIn("dms", db, "no migration tools for a database that is already Aurora")
        self.assertNotIn("mma-workshop", db, "no Oracle workshop without Oracle")
        genai = {a["id"] for a in sel.get("genai", {}).get("assets", [])} | {s["id"] for s in sel.get("genai", {}).get("services", [])}
        self.assertNotIn("agentcore", genai, "no agent runtime for a plain chatbot")
        self.assertNotIn("strands-agentcore-skill", genai)
        devops = {s["id"] for s in sel["modern-devops"]["services"]}
        self.assertNotIn("continuum", devops, "preview-stage agents are not in the catalog; the report must not read as a product announcement")
        cands[1]["rationale"] = "multi-agent assistant with MCP server tool calls"
        sel = self.ns.select(cands, checks, inventory)
        self.assertIn("agentcore", {s["id"] for s in sel["genai"]["services"]})
        # The word "migration" in a sentence about moving a chatbot to Fargate is not a reason to offer database tooling.
        cands[0]["rationale"] = "keep Aurora; the chatbot migration to Fargate is covered under containers"
        sel = self.ns.select(cands, checks, inventory)
        db = {s["id"] for s in sel["managed-database"]["services"]} | {a["id"] for a in sel["managed-database"]["assets"]}
        self.assertFalse({"dms", "db-migration-skill", "db-migration-workshop"} & db, db)
        cands[0]["rationale"] = "migrate the self-managed Oracle database on EC2 to Aurora PostgreSQL"
        inventory["resources"][0]["attributes"]["Engine"] = "oracle-ee"
        sel = self.ns.select(cands, checks, inventory)
        db = {s["id"] for s in sel["managed-database"]["services"]} | {a["id"] for a in sel["managed-database"]["assets"]}
        self.assertTrue({"dms", "dbmig-aidlc", "mma-workshop"} <= db)
        self.assertNotIn("aidlcops", {a["id"] for a in sel["modern-devops"]["assets"]}, "no always-on assets; the AIDLC sample needs pipeline findings")
        for pathway, groups in sel.items():
            for item in groups["services"] + groups["assets"]:
                self.assertTrue(item["because"], item["id"])


class RunMetadataImport(unittest.TestCase):
    def test_kiro_session_import_reads_credits_requests_elapsed_and_model(self):
        rm = load("rq_run_metadata", "skills/aws-mcp-setup/scripts/run_metadata.py")
        with tempfile.TemporaryDirectory() as tmp:
            home, project = Path(tmp) / "home", Path(tmp) / "proj"
            sess = home / ".kiro/sessions/abc/sess_1"
            (sess / "sub-executions").mkdir(parents=True); project.mkdir()
            (sess / "session.json").write_text(json.dumps({"workspacePaths": [str(project)], "title": "moda run", "lastModifiedAt": "2026-11-09T08:00:00Z"}))
            lines = [{"timestamp": "2026-11-09T07:30:00Z", "payload": {"type": "assistant", "reasoningModelId": "qdev::claude-opus-5.5"}},
                     {"timestamp": "2026-11-09T07:31:00Z", "payload": {"type": "usage_summary", "elapsedTime": 90000, "requestIds": ["a", "b"], "promptTurnSummaries": [{"usage": 1.5}]}},
                     {"timestamp": "2026-11-09T07:40:00Z", "payload": {"type": "usage_summary", "elapsedTime": 30000, "requestIds": ["c"], "promptTurnSummaries": [{"usage": 0.5}]}}]
            (sess / "messages.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
            (sess / "sub-executions/x.jsonl").write_text(json.dumps({"timestamp": "2026-11-09T07:35:00Z", "payload": {"type": "usage_summary", "elapsedTime": 6000, "requestIds": ["d"], "promptTurnSummaries": [{"usage": 0.25}]}}))
            with unittest.mock.patch.object(rm.Path, "home", return_value=home):
                file = project / "run-metadata.json"
                rm.cmd_init(argparse.Namespace(output=str(file), client="kiro-cli", model=None, note=None, force=False))
                result = rm.cmd_import_kiro_session(argparse.Namespace(file=str(file), project_root=str(project), label="stages 1-5", since=None, until=None, last=1))
            self.assertEqual(result["sessions"][0]["requests"], 4)
            self.assertEqual(result["sessions"][0]["credits"], 2.25)
            self.assertEqual(result["sessions"][0]["elapsed_seconds"], 126.0)
            self.assertEqual(result["sessions"][0]["models"], ["claude-opus-5.5"])
            kit = load("rq_kit", "skills/aws-modernization-pathway/scripts/report_kit.py")
            meta = kit.load_run_metadata(file)
            self.assertEqual(meta["totals"]["requests"], 4)
            self.assertIsNone(meta["totals"]["total_tokens"], "Kiro V3 records no tokens; never estimated")
            self.assertIn(("Kiro credits", "2.2"), kit.run_metadata_rows(meta))
            # Kiro records a turn's usage only when the turn ends. The import made before the long turn ended under-counts;
            # importing the same session again replaces that entry instead of adding to it (the live run double-counted).
            lines.append({"timestamp": "2026-11-09T09:00:00Z", "payload": {"type": "usage_summary", "elapsedTime": 3600000, "requestIds": [f"r{i}" for i in range(300)], "promptTurnSummaries": [{"usage": 400.0}]}})
            (sess / "messages.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
            with unittest.mock.patch.object(rm.Path, "home", return_value=home):
                rm.cmd_import_kiro_session(argparse.Namespace(file=str(file), project_root=str(project), label="assessment", since=None, until=None, last=1))
            meta = kit.load_run_metadata(file)
            self.assertEqual(len(meta["entries"]), 1, "the same session is one entry")
            self.assertEqual(meta["totals"]["requests"], 304)
            self.assertIn(("Kiro credits", "402.2"), kit.run_metadata_rows(meta))


class UsageAutoImport(unittest.TestCase):
    """The report records the AI tool, model and tokens (or Kiro credits) from the client's own session files: no agent
    step, no estimate, and a re-import after every turn never double-counts."""

    @staticmethod
    def kiro(home, project, name, records, modified="2020-01-01T09:00:00Z"):
        sess = home / f".kiro/sessions/h/{name}"
        sess.mkdir(parents=True)
        (sess / "session.json").write_text(json.dumps({"workspacePaths": [str(project)], "title": "moda", "lastModifiedAt": modified}))
        (sess / "messages.jsonl").write_text("\n".join(json.dumps(r) for r in records))
        return sess

    @staticmethod
    def turn(stamp, credits, requests=1, ms=60000):
        return {"timestamp": stamp, "payload": {"type": "usage_summary", "elapsedTime": ms, "requestIds": [f"{stamp}{i}" for i in range(requests)],
                                                "promptTurnSummaries": [{"usage": credits}]}}

    def test_each_client_is_found_from_the_run_folder(self):
        rm = load("ua_run_metadata", "skills/aws-mcp-setup/scripts/run_metadata.py")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            # Kiro CLI: two sessions (the run was resumed), and a later run of the same workload bounds the window.
            kiro_project = Path(tmp) / "kiro-proj"; run = kiro_project / "artifacts/shop/20200101T070000Z"; run.mkdir(parents=True)
            (kiro_project / "artifacts/shop/20200101T080000Z").mkdir()
            model = {"timestamp": "2020-01-01T07:01:00Z", "payload": {"type": "assistant", "reasoningModelId": "qdev::model-k"}}
            self.kiro(home, kiro_project, "sess_a", [model, self.turn("2020-01-01T07:10:00Z", 10.0, 3)])
            self.kiro(home, kiro_project, "sess_b", [self.turn("2020-01-01T07:40:00Z", 5.5, 2), self.turn("2020-01-01T08:30:00Z", 99.0, 50)])
            self.kiro(home, kiro_project, "sess_old", [self.turn("2019-12-31T07:00:00Z", 77.0)], modified="2019-12-31T08:00:00Z")
            # Claude Code: the project folder name with every non-alphanumeric character as '-'.
            claude_project = Path(tmp) / "claude.proj"; claude_run = claude_project / "artifacts/shop/20200101T070000Z"; claude_run.mkdir(parents=True)
            folder = home / ".claude/projects" / "".join(c if c.isalnum() else "-" for c in str(claude_project.resolve())); folder.mkdir(parents=True)
            (folder / "s1.jsonl").write_text(json.dumps({"type": "assistant", "uuid": "a1", "timestamp": "2020-01-01T07:05:00Z",
                                                         "message": {"id": "m1", "model": "model-c", "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 40}}}) + "\n")
            # Codex: rollouts whose session_meta cwd is the project; another project's rollout is ignored.
            codex_project = Path(tmp) / "codex-proj"; codex_run = codex_project / "artifacts/shop/20200101T070000Z"; codex_run.mkdir(parents=True)
            day = home / ".codex/sessions/2020/01/01"; day.mkdir(parents=True)
            for name, cwd, tokens in (("rollout-a.jsonl", codex_project, 1000), ("rollout-b.jsonl", Path(tmp) / "elsewhere", 99999)):
                lines = [{"timestamp": "2020-01-01T07:00:00Z", "type": "session_meta", "payload": {"id": name, "cwd": str(cwd), "source": "cli"}},
                         {"timestamp": "2020-01-01T07:00:01Z", "type": "turn_context", "payload": {"model": "model-x"}},
                         {"timestamp": "2020-01-01T07:02:00Z", "type": "token_usage_record", "payload": {"response_id": "r1", "usage": {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 7}}}]
                (day / name).write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            args = lambda run_dir: argparse.Namespace(run_dir=str(run_dir), file=None, project_root=None, since=None, transcript=None, client=None, label=None)
            with patch.object(rm.Path, "home", return_value=home):
                first = rm.cmd_import_auto(args(run))
                again = rm.cmd_import_auto(args(run))
                claude = rm.cmd_import_auto(args(claude_run))
                codex = rm.cmd_import_auto(args(codex_run))
                with self.assertRaisesRegex(ValueError, "not a run folder"):
                    rm.cmd_import_auto(args(Path(tmp) / "kiro-proj"))
            self.assertEqual((first["client"], sorted(first["sessions"]), first["until"]), ("kiro-cli", ["sess_a", "sess_b"], "2020-01-01T08:00:00Z"))
            meta = rm.report_kit.load_run_metadata(run / "run-metadata.json")
            self.assertEqual(len(meta["entries"]), 2, "two sessions, each imported once although import-auto ran twice")
            self.assertEqual(again["totals"]["requests"], 5, "the next run's turn and the earlier day's session are not counted")
            self.assertEqual(sum(e["breakdown"]["credits"] for e in meta["entries"]), 15.5)
            self.assertEqual((meta["agent_client"], meta["models_seen"]), ("kiro-cli", ["model-k"]))
            c = rm.report_kit.load_run_metadata(claude_run / "run-metadata.json")
            self.assertEqual((claude["client"], c["totals"]["input_tokens"], c["totals"]["output_tokens"], c["models_seen"]), ("claude-code", 1000, 40, ["model-c"]))
            x = rm.report_kit.load_run_metadata(codex_run / "run-metadata.json")
            self.assertEqual((codex["client"], x["totals"]["input_tokens"], codex["sessions"]), ("codex", 1000, ["rollout-a.jsonl"]))

    def test_usage_block_in_each_language_and_refresh_after_the_last_turn(self):
        report = load("ua_assessment_report", "skills/aws-modernization-pathway/scripts/assessment_report.py")
        kit = report.kit
        with tempfile.TemporaryDirectory() as tmp:
            home, project = Path(tmp) / "home", Path(tmp) / "proj"
            run = project / "artifacts/shop/20200101T070000Z"
            sess = self.kiro(home, project, "sess_a", [{"timestamp": "2020-01-01T07:01:00Z", "payload": {"type": "assistant", "reasoningModelId": "model-k"}},
                                                       self.turn("2020-01-01T07:10:00Z", 1.25, 2, ms=126000)])
            stale = "<!--moda-usage-->stale<!--/moda-usage-->"
            for folder in ("06-assessment", "06-assessment-ko"):
                (run / folder).mkdir(parents=True)
                (run / folder / "assessment.html").write_text(f"<html><body><p>before</p>{stale}<p>after</p></body></html>")
                (run / folder / "assessment.md").write_text(f"# r\n\n{stale}\n\nafter\n")
            (run / "06-assessment/assessment-summary.json").write_text(json.dumps({"status": "complete", "run_metadata": None}))
            with patch.object(Path, "home", return_value=home):
                first = report.refresh_usage(run)
                # the final turn ends after the report was written: the next refresh counts it
                (sess / "messages.jsonl").write_text((sess / "messages.jsonl").read_text() + "\n" + json.dumps(self.turn("2020-01-01T07:50:00Z", 3.0, 4)))
                report.refresh_usage(run)
            self.assertEqual(len(first["reports"]), 4)
            en = (run / "06-assessment/assessment.html").read_text()
            self.assertNotIn("stale", en); self.assertIn("<p>after</p>", en)
            for text in ("Kiro CLI", "model-k", "4.25", "Kiro credits", "Model requests", "Kiro CLI records credits, not tokens."):
                self.assertIn(text, en)
            ko = (run / "06-assessment-ko/assessment.html").read_text()
            for text in ("AI \ub3c4\uad6c", "Kiro \ud06c\ub808\ub527", "\ubaa8\ub378 \uc694\uccad \uc218", "4.25"):
                self.assertIn(text, ko)
            self.assertNotIn("Model requests", ko); self.assertNotIn("min", ko.replace("<!--", ""), "durations use the glossary units")
            self.assertIn("- Kiro credits: 4.25", (run / "06-assessment/assessment.md").read_text())
            self.assertIn("4.25", (run / "06-assessment-ko/assessment.md").read_text())
            self.assertEqual(json.loads((run / "06-assessment/assessment-summary.json").read_text())["run_metadata"]["totals"]["requests"], 6)
        tokens = {"entries": [{"breakdown": {"cache_read_input": 900}, "recorded_at": "2020-01-01T08:00:00Z"}], "totals": {"input_tokens": 1000, "output_tokens": 40, "requests": 3,
                  "inference_seconds": 3725}, "models_seen": ["model-c"], "agent_client": "claude-code"}
        html = kit.usage_block_html(tokens)
        self.assertIn("1,000 (900 of which cached)", html); self.assertIn("Claude Code", html); self.assertIn("1 h 2 min", html)
        self.assertNotIn("records credits", html)
        self.assertIn("Usage of the AI assistant: not recorded", kit.usage_block_markdown(None))

    def test_stop_hook_refreshes_the_usage_of_a_finished_run(self):
        hook = load("ua_hook", "skills/moda-skill/scripts/hook.py")
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "artifacts/shop/20200101T070000Z"
            (run / "06-assessment").mkdir(parents=True)
            transcript = Path(tmp) / "t.jsonl"; transcript.write_text("{}\n")
            calls = []
            with patch("subprocess.run", side_effect=lambda command, **kw: calls.append(command)):
                hook.refresh_usage(run, {"transcript_path": str(transcript)})  # no report yet: nothing to refresh
                (run / "06-assessment/assessment.html").write_text("<html></html>")
                hook.refresh_usage(run, {"transcript_path": str(transcript)})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2:5], ["refresh-usage", "--run-dir", str(run)])
            self.assertEqual(calls[0][-2:], ["--transcript", str(transcript)])
            with patch("subprocess.run", side_effect=OSError("boom")):
                hook.refresh_usage(run, {})  # never fails the stop


class ModelGuidance(unittest.TestCase):
    def test_tiers_and_exit_codes(self):
        moda = load("rq_moda", "skills/moda-skill/scripts/moda.py")
        self.assertEqual(moda.model_tier("claude-opus-5.5"), "frontier")
        self.assertEqual(moda.model_tier("claude-sonnet-4.6"), "reduced")
        self.assertEqual(moda.model_tier("claude-haiku-4.5"), "unsuitable")
        self.assertEqual(moda.model_tier("something-new"), "unknown")
        self.assertTrue(moda.model_check("claude-opus-5.5", "high", "kiro-cli")["ok"])
        low = moda.model_check("claude-sonnet-4.6", "high", "kiro-cli")
        self.assertFalse(low["ok"]); self.assertIn("claude-opus-5.5", low["message"]); self.assertIn("shallower", low["message"])
        effort = moda.model_check("claude-opus-5.5", "medium", "kiro-cli")
        self.assertFalse(effort["ok"]); self.assertIn("Effort is 'medium'", effort["message"])
        run = run_python(str(ROOT / "skills/moda-skill/scripts/moda.py"), "model-check", "--model", "claude-sonnet-4.6", capture_output=True, text=True)
        self.assertEqual(run.returncode, 3)
        kit = load("rq_kit2", "skills/aws-modernization-pathway/scripts/report_kit.py")
        self.assertEqual(kit.model_tier("claude-sonnet-4.6"), "reduced")
        self.assertEqual(kit.MODEL_RECOMMENDED, moda.MODELS["recommended"][0], "report_kit and models.json agree on the recommended model")


class LocalizationLabels(unittest.TestCase):
    def test_preserved_or_half_translated_labels_are_rejected(self):
        terms = {"Why it matters": "왜 중요한가", "Target": "대상"}
        self.assertEqual(localize.untranslated_label("Target", terms, "ko"), "Target")
        self.assertEqual(localize.untranslated_label("Why it matters: 고객이 먼저 장애를 겪습니다", terms, "ko"), "Why it matters")
        self.assertIsNone(localize.untranslated_label("Target", terms, "en"))
        self.assertIsNone(localize.untranslated_label("shopnow-alerts", terms, "ko"))


if __name__ == "__main__":
    unittest.main()
