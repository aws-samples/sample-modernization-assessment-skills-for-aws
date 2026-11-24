"""Quantitative evidence per recommendation (benefits.py): computed from the run's evidence, never estimated."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import run_python  # noqa: E402
from test_specialists import load, good_advice, write_review, CLUSTER  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BENEFITS = ROOT / "skills/aws-modernization-pathway/scripts/benefits.py"
REPORT = ROOT / "skills/aws-modernization-pathway/scripts/assessment_report.py"
RECOMMEND = ROOT / "skills/aws-modernization-pathway/scripts/recommend.py"
benefits = load("benefits_test", "skills/aws-modernization-pathway/scripts/benefits.py")
advice = load("advice_for_benefits", "skills/aws-modernization-pathway/scripts/advice.py")

ACCOUNT, REGION = "111111111111", "us-east-1"
WRITER = f"{ACCOUNT}:{REGION}:rds.instance:orders-writer"
SVC_A = f"{ACCOUNT}:{REGION}:ecs.service:arn:aws:ecs:{REGION}:{ACCOUNT}:service/shop/catalog-api"
SVC_B = f"{ACCOUNT}:{REGION}:ecs.service:arn:aws:ecs:{REGION}:{ACCOUNT}:service/shop/order-api"
HOST = f"{ACCOUNT}:{REGION}:ec2.instance:i-0chatbot"
PIPE = f"{ACCOUNT}:{REGION}:codepipeline.pipeline:shop-catalog"
BILL = f"{ACCOUNT}:global:ce.cost-by-service:account"


def res(rid, kind, attributes, level="workload"):
    native = rid.split(f":{kind}:", 1)[1]
    return {"id": rid, "type": kind, "native_id": native, "name": native.rsplit("/", 1)[-1],
            "account_id": ACCOUNT, "region": REGION, "attributes": attributes, "evidence_ids": ["e0001"], "observations": {}, "level": level}


def build(root):
    """A run with an Aurora cluster on one writer, two ECS services (one deployed by a pipeline), a chatbot host, a
    pipeline with three runs, the account bill and two specialist reviews."""
    snap = root / "03-as-is-enriched"
    snap.mkdir(parents=True)
    resources = [
        res(CLUSTER, "rds.cluster", {"DBClusterIdentifier": "orders-db", "Engine": "aurora-postgresql", "MultiAZ": False, "DBClusterMembers": [{"DBInstanceIdentifier": "orders-writer", "IsClusterWriter": True}]}),
        res(WRITER, "rds.instance", {"DBInstanceIdentifier": "orders-writer", "AvailabilityZone": "us-east-1b", "DBClusterIdentifier": "orders-db", "MultiAZ": False}),
        res(SVC_A, "ecs.service", {"serviceName": "catalog-api", "desiredCount": 2}),
        res(SVC_B, "ecs.service", {"serviceName": "order-api", "desiredCount": 2}),
        res(HOST, "ec2.instance", {"InstanceId": "i-0chatbot", "InstanceType": "t3.large", "Placement": {"AvailabilityZone": "us-east-1a"}}),
        res(PIPE, "codepipeline.pipeline", {"name": "shop-catalog"}),
        res(BILL, "ce.cost-by-service", {"months": [{"start": "2026-10-01", "total_usd": 900.0, "services": [
            {"service": "Amazon Relational Database Service", "usd": 210.4}, {"service": "Amazon Elastic Container Service", "usd": 61.2},
            {"service": "Amazon Elastic Compute Cloud - Compute", "usd": 60.7}, {"service": "Amazon OpenSearch Service", "usd": 263.6}]}]}, level="account")]
    (snap / "inventory.json").write_text(json.dumps({"schema_version": "1.0", "account_id": ACCOUNT, "run_id": "20261101T000000Z", "regions": [REGION],
                                                     "services": ["rds", "ecs", "ec2", "codepipeline"], "resources": resources}))
    (snap / "collection-manifest.json").write_text(json.dumps({"completed_at": "2026-11-23T07:40:00+00:00", "status": "complete", "coverage": {"ok": 1},
                                                             "calls": [{"id": "e0001", "service": "rds", "operation": "describe-db-clusters"}]}))
    (snap / "scope-resources.json").write_text(json.dumps({"resources": [{"id": r["id"]} for r in resources if r["level"] != "account"]}))
    sup = root / "03-supplementary"
    sup.mkdir()
    pipeline = {"pipeline": {"name": "shop-catalog", "stages": [{"name": "Source", "actions": [{"name": "Source", "actionTypeId": {"provider": "CodeCommit"}, "configuration": {}}]},
                                                               {"name": "Deploy", "actions": [{"name": "Deploy", "actionTypeId": {"provider": "ECS"}, "configuration": {"ClusterName": "shop", "ServiceName": "catalog-api"}}]}]}}
    runs = {"pipelineExecutionSummaries": [
        {"status": "Succeeded", "startTime": "2026-11-10T10:00:00+09:00", "lastUpdateTime": "2026-11-10T10:06:00+09:00"},
        {"status": "Failed", "startTime": "2026-11-09T10:00:00+09:00", "lastUpdateTime": "2026-11-09T10:01:00+09:00"},
        {"status": "Succeeded", "startTime": "2026-11-08T10:00:00+09:00", "lastUpdateTime": "2026-11-08T10:04:00+09:00"}]}
    (sup / "s0001-codepipeline-get-pipeline.json").write_text(json.dumps(pipeline))
    (sup / "s0002-codepipeline-list-pipeline-executions.json").write_text(json.dumps(runs))
    (sup / "supplementary-manifest.json").write_text(json.dumps({"records": [
        {"id": "s0001", "operation": "get-pipeline", "arguments": ["--name", "shop-catalog"], "status": "ok", "raw_path": "s0001-codepipeline-get-pipeline.json", "raw_format": "json"},
        {"id": "s0002", "operation": "list-pipeline-executions", "arguments": ["--pipeline-name", "shop-catalog", "--max-items", "20"], "status": "ok",
         "raw_path": "s0002-codepipeline-list-pipeline-executions.json", "raw_format": "json"}]}))
    (root / "04-review").mkdir()
    checks = [{"id": "f0001", "rule_id": "AURORA_SINGLE_INSTANCE", "pillar": "Reliability", "resource_id": CLUSTER, "status": "FAIL", "severity": "HIGH", "evidence_ids": ["e0001"],
               "observation": "one writer", "recommendation": "add a reader"},
              {"id": "f0002", "rule_id": "CI_TEST_GATE", "pillar": "Operational Excellence", "resource_id": SVC_B, "status": "WARN", "severity": "MEDIUM", "evidence_ids": ["e0001"],
               "observation": "order-api has no pipeline", "recommendation": "add a pipeline"},
              {"id": "f0003", "rule_id": "RDS_ENGINE_VERSION_SUPPORT", "pillar": "Operational Excellence", "resource_id": CLUSTER, "status": "WARN", "severity": "MEDIUM",
               "evidence_ids": ["e0001"], "observation": "aurora-postgresql 13.9 standard support ends 2027-02-28", "recommendation": "plan the upgrade"},
              {"id": "f0004", "rule_id": "EC2_IMDSV2", "pillar": "Security", "resource_id": HOST, "status": "WARN", "severity": "MEDIUM", "evidence_ids": ["e0001"],
               "observation": "IMDSv1 allowed on 2026-11-06 launch", "recommendation": "require IMDSv2"}]
    (root / "04-review/findings.json").write_text(json.dumps({"schema_version": "1.0", "account_id": ACCOUNT, "run_id": "20261101T000000Z",
                                                              "as_is_snapshot": {"path": "03-as-is-enriched"}, "checks": checks}))
    (root / "scope.json").write_text(json.dumps({"workload_name": "orders", "output_language": "en", "auth": {"profile": "assess"}}))
    (root / "05-modernization").mkdir()
    (root / "05-modernization/list-prices.json").write_text(json.dumps({"prices": []}))
    db = good_advice()
    db["recommendations"][0]["refs"] = ["f0001", "f0003"]
    write_review(root, "database", db)
    ops = good_advice("devops", "modern-devops")
    ops["recommendations"][0].update(title="Deploy order-api through a pipeline", refs=["f0002", SVC_B], list_prices=[], cost_direction="neutral",
                                     change="Add a pipeline for order-api like the catalog one.", benefit="Every change to order-api goes through the same gates.")
    ops["current_state"] = [{"text": "One of two services has a pipeline.", "refs": ["f0002"]}]
    ops["not_now"] = []
    ops["handoffs"] = [{"to": "database", "text": "Database alarms follow the database review."}]
    write_review(root, "devops", ops, graph={"nodes": [{"id": "n1", "label": "order-api", "state": "changed", "resource_ids": [SVC_B]}, {"id": "n2", "label": "pipeline", "state": "proposed"}],
                                            "edges": [{"id": "x1", "source": "n2", "target": "n1", "label": "deploys", "state": "proposed"}]})
    merged = advice.merge(advice.Context(root), allow_missing=True)
    assert merged["status"] != "fail", merged
    return root


class BenefitsTests(unittest.TestCase):
    def test_rows_are_computed_from_the_evidence_and_never_estimated(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build(Path(tmp))
            result = benefits.compute(run)
            by = {r["specialist"]: {e["metric"]: e for e in r["evidence"]} for r in result["recommendations"]}
            db, ops = by["database"], by["devops"]
            self.assertEqual(db["Findings this change addresses"]["value"], "2 failed or warned (1 high, 1 medium)")
            self.assertEqual(db["Findings this change addresses"]["refs"], ["f0001", "f0003"])
            self.assertIn("orders-db (us-east-1b)", db["Single-AZ or single-node components among them"]["value"], "the cluster's only member sits in one AZ")
            self.assertEqual(db["Database engine standard support ends"]["value"], "2027-02-28 (97 days after collection)")
            bill = db["Account bill for the services these resources use, 2026-10"]
            self.assertEqual(bill["value"], "$210/month"); self.assertIn("whole account", bill["source"]); self.assertNotIn("OpenSearch", bill["source"])
            self.assertEqual(db["Cost of one hour without these components"]["kind"], "input_needed")
            self.assertEqual(db["Cost of one hour without these components"]["value"], "owner to provide")
            self.assertEqual(ops["ECS services deployed by a pipeline"]["value"], "1 of 2 (catalog-api); without: order-api")
            self.assertEqual(ops["ECS services deployed by a pipeline"]["refs"], ["s0001"])
            self.assertEqual(ops["Pipeline history shop-catalog"]["value"], "3 run(s), 1 failed, successful runs take 5 min (median), last 2026-11-10")
            self.assertIn("multiplies the 1 service(s) without a pipeline", ops["Hours per manual deployment × deployments per month"]["source"])
            # A launch date in a non-dated rule is not a deadline.
            self.assertFalse(any("2026-11-06" in e["value"] for r in result["recommendations"] for e in r["evidence"]))
            # Nothing estimated: no hours, savings or percentages anywhere in the values.
            for r in result["recommendations"]:
                for e in r["evidence"]:
                    self.assertIsNone(re.search(r"(?i)\d+(\.\d+)?\s?%|\bsav(e|ing)|\bhours? saved|person", e["value"]), e)
                    self.assertTrue(e["source"], e)

    def test_report_shows_the_rows_in_the_quantitative_evidence_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build(Path(tmp))
            # minimum stage-5 inputs the renderer needs
            r = run_python(str(RECOMMEND), "--inventory", str(run / "03-as-is-enriched/inventory.json"), "--findings", str(run / "04-review/findings.json"),
                           "--workload", "orders", "--output", str(run / "05-modernization"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            (run / "04-review/scorecard.json").write_text(json.dumps({"Reliability": {"score": 50, "counts": {"FAIL": 1}, "evidence_coverage_percent": 100}}))
            report = run_python(str(REPORT), "--run-dir", str(run), "--workload", "orders", capture_output=True, text=True)
            self.assertEqual(report.returncode, 0, report.stderr)
            html = (run / "06-assessment/assessment.html").read_text()
            self.assertIn("<th>Quantitative evidence</th>", html)
            self.assertIn("1 of 2 (catalog-api); without: order-api", html)
            self.assertIn('class="input-needed"', html)
            md = (run / "06-assessment/assessment.md").read_text()
            self.assertIn("| Pipeline history shop-catalog | 3 run(s), 1 failed", md)
            # Evidence a specialist cites is in the evidence index and links back to its section on the report page.
            details = (run / "06-assessment/details.html").read_text()
            self.assertIn('href="assessment.html#sp-database">Database</a>', details)
            for page in (html, details):
                ids = set(re.findall(r'\bid="([^"]+)"', page))
                self.assertEqual([h for h in re.findall(r'href="#([^"]+)"', page) if h not in ids], [])
            written = json.loads((run / "06-assessment/benefits.json").read_text())
            self.assertEqual(len(written["recommendations"]), 2)
            cli = run_python(str(BENEFITS), "--run-dir", str(run), capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertTrue((run / "05-modernization/benefits.json").is_file())

    def test_a_review_cannot_write_its_own_evidence_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build(Path(tmp))
            adv = good_advice()
            adv["recommendations"][0]["evidence"] = [{"metric": "hours saved", "value": "40", "source": "estimate"}]
            write_review(run, "database", adv)
            errors = " ".join(advice.validate(advice.Context(run), "database")["errors"])
            self.assertIn("written by benefits.py", errors)


if __name__ == "__main__":
    unittest.main()
