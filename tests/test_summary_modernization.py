"""Executive summary leads with modernization: the seven directions and two foundations, the inserted pathways of an older
candidates file, the top recommendations and the summary order (offline fixtures)."""
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/aws-modernization-pathway/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run_python  # noqa: E402
from test_workflow import fixture  # noqa: E402


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recommend = load("recommend_for_summary", "skills/aws-modernization-pathway/scripts/recommend.py")
report = load("report_for_summary", "skills/aws-modernization-pathway/scripts/assessment_report.py")
REVIEW = ROOT / "skills/aws-architecture-review/scripts/review.py"
RECOMMEND = SCRIPTS / "recommend.py"
REPORT = SCRIPTS / "assessment_report.py"

ACCOUNT = "111111111111"
BASTION = f"{ACCOUNT}:us-east-1:ec2.instance:i-0bastion"
NODE = f"{ACCOUNT}:us-east-1:ec2.instance:i-0node"
APP = f"{ACCOUNT}:us-east-1:ec2.instance:i-0app"
DB = f"{ACCOUNT}:us-east-1:rds.instance:orders"


def instance(rid, tags, platform="Linux/UNIX", kind="t3.small"):
    return {"id": rid, "type": "ec2.instance", "native_id": rid.rsplit(":", 1)[-1], "name": rid.rsplit(":", 1)[-1], "account_id": ACCOUNT,
            "region": "us-east-1", "attributes": {"InstanceId": rid.rsplit(":", 1)[-1], "InstanceType": kind, "PlatformDetails": platform,
                                                  "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}, "evidence_ids": [], "observations": {}}


def legacy_run(root, license_finding=False):
    """A run whose candidates file predates Managed services and Open source: an EKS node, a bastion whose UserData installs
    kubectl, an application host, one PostgreSQL instance and a specialist recommendation that moves the host to Fargate."""
    snap = root / "03-as-is"
    snap.mkdir(parents=True)
    resources = [instance(BASTION, {"Name": "orders-bastion", "Role": "kubectl-bastion"}),
                 instance(NODE, {"Name": "orders-eks-node", "eks:cluster-name": "orders"}, kind="m5.large"),
                 instance(APP, {"Name": "orders-worker"}, kind="t3.large"),
                 {"id": DB, "type": "rds.instance", "native_id": "orders", "name": "orders", "account_id": ACCOUNT, "region": "us-east-1",
                  "attributes": {"Engine": "postgres", "LicenseModel": "postgresql-license"}, "evidence_ids": [], "observations": {}}]
    common = {"schema_version": "1.0", "run_id": "legacy", "account_id": ACCOUNT, "regions": ["us-east-1"], "services": ["ec2", "rds"]}
    (snap / "inventory.json").write_text(json.dumps(dict(common, collection_status="complete", resources=resources, relationships=[])))
    checks = [{"id": "f0001", "rule_id": "EC2_SINGLE_INSTANCE_NO_ASG", "pillar": "Reliability", "status": "WARN", "severity": "MEDIUM", "resource_id": APP,
               "observation": "orders-worker runs alone", "recommendation": "add an Auto Scaling group"}]
    if license_finding:
        checks.append({"id": "f0002", "rule_id": "COMMERCIAL_LICENSE_DEPENDENCY", "pillar": "Cost Optimization", "status": "WARN", "severity": "LOW",
                       "resource_id": APP, "observation": "i-0app runs Windows Server (license included, usage operation RunInstances:0002).",
                       "recommendation": "Price the open-source alternative."})
    (root / "04-review").mkdir()
    (root / "04-review/findings.json").write_text(json.dumps(dict(common, checks=checks)))
    supplementary = root / "03-supplementary"
    supplementary.mkdir()
    (supplementary / "s0001.json").write_text(json.dumps({"InstanceId": "i-0bastion", "UserData": {"Decoded": "#!/bin/bash\ncurl -o /usr/local/bin/kubectl https://dl.k8s.io/x\n"}}))
    (supplementary / "supplementary-manifest.json").write_text(json.dumps({"records": [
        {"id": "s0001", "operation": "describe-instance-attribute", "status": "ok", "raw_path": "s0001.json"}]}))
    out = root / "05-modernization"
    out.mkdir()
    (out / "specialist-advice.json").write_text(json.dumps({"specialists": [{"specialist": "containers", "role": "Container & Serverless Specialist SA Agent",
        "pathways": ["container", "serverless", "cloud-native"], "current_state": [],
        "recommendations": [{"title": "Move the worker to a Fargate service", "pathway": "container", "refs": ["f0001"],
                             "change": "Package the worker as an image and run it as a Fargate service in two AZs."}]}]}))
    legacy = [c for c in recommend.bootstrap() if c["pathway"] not in recommend.ADDED_PATHWAYS]
    (root / "candidates.json").write_text(json.dumps(legacy))
    return root


class DirectionCatalogTests(unittest.TestCase):
    def test_seven_directions_then_foundations_in_one_order(self):
        self.assertEqual(recommend.DIRECTIONS, ("container", "serverless", "managed-services", "managed-database", "managed-analytics", "genai", "open-source"))
        self.assertEqual(recommend.FOUNDATIONS, ("modern-devops", "cloud-native"))
        self.assertEqual(list(recommend.PATHWAYS), list(recommend.DIRECTIONS) + list(recommend.FOUNDATIONS))
        self.assertEqual([c["pathway"] for c in recommend.bootstrap()], list(recommend.PATHWAYS))
        self.assertEqual(set(report.PATHWAYS), set(recommend.PATHWAYS), "the report labels every pathway")
        self.assertEqual([report.PATHWAYS[k] for k in recommend.DIRECTIONS],
                         ["Containers", "Serverless", "Managed services", "Managed database", "Managed analytics", "Generative AI", "Open source"])
        for key in recommend.ADDED_PATHWAYS:
            self.assertTrue(recommend.PUBLIC_SOURCES[key].startswith("https://docs.aws.amazon.com/"))
            self.assertTrue(recommend.QUESTIONS[key])
        # Section 4 lists the seven in order, then a Foundations row, then the foundations; ids anchor the summary tiles.
        shuffled = list(reversed([dict(c, problem="p", rationale="r") for c in recommend.bootstrap()]))
        table = report.pathway_table(shuffled, [])
        order = re.findall(r'<tr id="dir-([a-z-]+)"', table)
        self.assertEqual(order, list(recommend.DIRECTIONS) + list(recommend.FOUNDATIONS))
        self.assertLess(table.index('id="dir-open-source"'), table.index(report.FOUNDATIONS_LABEL))
        self.assertLess(table.index(report.FOUNDATIONS_LABEL), table.index('id="dir-modern-devops"'))
        cards = report.pathway_cards(shuffled)
        self.assertEqual(re.findall(r'id="dir-([a-z-]+)"', cards), list(recommend.DIRECTIONS) + list(recommend.FOUNDATIONS))
        # The specialists cover the new directions.
        advice = load("advice_for_summary", "skills/aws-modernization-pathway/scripts/advice.py")
        self.assertEqual(advice.PATHWAY_OWNER["managed-services"], "containers")
        self.assertEqual(advice.PATHWAY_OWNER["open-source"], "database")
        self.assertEqual(set(advice.PATHWAY_OWNER), set(recommend.PATHWAYS))


class LegacyCandidatesTests(unittest.TestCase):
    def run_recommend(self, root):
        source = root / "candidates.json"
        original = source.read_bytes()
        result = run_python(str(RECOMMEND), "--inventory", str(root / "03-as-is/inventory.json"), "--findings", str(root / "04-review/findings.json"),
                            "--candidates", str(source), "--workload", "orders", "--output", str(root / "05-modernization"), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.read_bytes(), original, "the input file is not rewritten")
        return json.loads((root / "05-modernization/pathways.json").read_text())

    def test_old_seven_key_file_loads_with_evidence_rationales(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = self.run_recommend(legacy_run(Path(tmp)))
            by = {c["pathway"]: c for c in doc["candidates"]}
            self.assertEqual(set(by), set(recommend.PATHWAYS))
            self.assertTrue(any("Managed Services" in n and "Open Source" in n for n in doc["input_notes"]), doc["input_notes"])
            services, source = by["managed-services"], by["open-source"]
            self.assertEqual((services["status"], source["status"]), ("not_assessed", "not_assessed"))
            text = services["rationale"]
            self.assertTrue(text.startswith("2 in-scope EC2 instances run outside the EKS node groups: orders-bastion and orders-worker."), text)
            self.assertIn("Role tag kubectl-bastion, UserData names kubectl", text)
            self.assertIn("1 EKS worker node is left out.", text)
            self.assertNotIn("orders-eks-node", text, "EKS worker nodes are not self-managed hosts here")
            self.assertIn("\u201cMove the worker to a Fargate service\u201d (Container & Serverless Specialist SA Agent)", text)
            self.assertIn(recommend.NOT_REVIEWED, text)
            self.assertEqual(sorted(services["resource_ids"]), sorted([BASTION, APP]))
            self.assertEqual(source["rationale"], "No license-included engine or operating system was found among the in-scope databases and instances: "
                             "1 RDS instance (postgres) and 3 EC2 instances (Linux/UNIX). " + recommend.NOT_REVIEWED)
            self.assertIsNone(services["rank"]); self.assertIsNone(services["score"])

    def test_license_findings_become_the_open_source_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = self.run_recommend(legacy_run(Path(tmp), license_finding=True))
            source = next(c for c in doc["candidates"] if c["pathway"] == "open-source")
            self.assertEqual(source["finding_ids"], ["f0002"])
            self.assertEqual(source["resource_ids"], [APP])
            self.assertTrue(source["rationale"].startswith("1 license-included engine or operating system found in scope: i-0app runs Windows Server "
                                                           "(license included, usage operation RunInstances:0002) (f0002)."), source["rationale"])

    def test_without_evidence_context_the_open_question_stays(self):
        legacy = [c for c in recommend.bootstrap() if c["pathway"] not in recommend.ADDED_PATHWAYS]
        rated = recommend.rank(legacy, set(), set())
        added = {c["pathway"]: c for c in rated if c["pathway"] in recommend.ADDED_PATHWAYS}
        self.assertEqual({k: c["rationale"] for k, c in added.items()}, {k: recommend.QUESTIONS[k] for k in recommend.ADDED_PATHWAYS})
        with self.assertRaises(ValueError):
            recommend.rank([c for c in legacy if c["pathway"] != "container"], set(), set())


def rec(title, pathway, effort, cost, findings, change="We suggest a change. It has a second sentence."):
    return {"title": title, "pathway": pathway, "effort": effort, "cost_direction": cost, "refs": [], "change": change, "benefit": "b",
            "rollback": "r", "alternatives": ["keep"], "current": "It is like this today. More detail follows.",
            "evidence": [{"metric": "Findings this change addresses", "value": f"{findings} failed or warned", "source": "findings.json", "kind": "count"},
                         {"metric": "Resources it touches", "value": "1: 1 ECS service", "source": "inventory.json", "kind": "count"}]}


class TopRecommendationTests(unittest.TestCase):
    def specialists(self):
        return [{"specialist": "containers", "role": "Container & Serverless Specialist SA Agent", "pathways": ["container", "serverless", "managed-services", "cloud-native"],
                 "recommendations": [rec("C1 weeks", "container", "weeks", "neutral", 9), rec("C2 days up", "container", "days", "up", 9),
                                     rec("C3 days neutral 2", "container", "days", "neutral", 2)]},
                {"specialist": "database", "role": "Database Specialist SA Agent", "pathways": ["managed-database", "open-source"],
                 "recommendations": [rec("D1 setting measure", "managed-database", "setting", "measure_first", 1), rec("D2 days down 2", "managed-database", "days", "down", 2),
                                     rec("D3 days neutral 5", "managed-database", "days", "neutral", 5)]},
                {"specialist": "devops", "role": "DevOps Specialist SA Agent", "pathways": ["modern-devops"],
                 "recommendations": [rec("O1 setting neutral", "modern-devops", "setting", "neutral", 0)]}]

    def test_selection_is_deterministic(self):
        statuses = {"container": "candidate", "managed-database": "conditional", "modern-devops": "not_assessed"}
        top = report.top_recommendations(self.specialists(), statuses)
        # Candidate directions first (effort, then cost group, then findings), then conditional, then the rest; five at most.
        self.assertEqual([r["title"] for _, _, r in top], ["C3 days neutral 2", "C2 days up", "C1 weeks", "D1 setting measure", "D3 days neutral 5"])
        statuses["container"] = "conditional"
        top = report.top_recommendations(self.specialists(), statuses)
        # Ties on everything but the specialist: the containers review (first in the specialist order) wins.
        self.assertEqual([r["title"] for _, _, r in top], ["D1 setting measure", "D3 days neutral 5", "C3 days neutral 2", "D2 days down 2", "C2 days up"])
        self.assertEqual(top, report.top_recommendations(list(self.specialists()), dict(statuses)), "same input, same pick")
        html = report.top_recommendations_html(self.specialists(), statuses, {})
        self.assertEqual(html.count('class="toprec"'), 5)
        self.assertIn('href="#rec-database-1"', html)
        self.assertIn("It is like this today.", html); self.assertNotIn("More detail follows.", html, "the summary shows the first sentence only")
        self.assertIn("Findings this change addresses</span> <b>5 failed or warned</b>", html)
        md = "\n".join(report.modernization_summary_markdown(recommend.bootstrap(), self.specialists(), statuses, {}))
        self.assertIn("1. **D1 setting measure**", md); self.assertIn("   - Proposed change: We suggest a change.", md)

    def test_moves_to_a_modern_target_come_first(self):
        """Field case: the rule put free settings and a DevOps item in the top five and left out the moves to Fargate.
        Moves (the specialist's kind, or the verb of an older review's title) of the seven directions come first."""
        specialists = self.specialists()
        specialists[0]["recommendations"].append(dict(rec("Move the chatbot to Fargate", "container", "days", "measure_first", 1)))
        specialists[1]["recommendations"].append(dict(rec("Open-source engine", "managed-database", "weeks", "up", 0), kind="move"))
        specialists[2]["recommendations"].insert(0, dict(rec("Migrate the pipeline", "modern-devops", "setting", "neutral", 3)))
        statuses = {"container": "conditional", "managed-database": "conditional", "modern-devops": "candidate"}
        titles = [r["title"] for _, _, r in report.top_recommendations(specialists, statuses)]
        self.assertEqual(titles[:2], ["Move the chatbot to Fargate", "Open-source engine"])
        self.assertNotIn("Migrate the pipeline", titles, "a foundation comes after the seven directions, even when it is a move")
        self.assertTrue(report.is_move({"title": "Consider consolidating the platform on ECS"}))
        self.assertFalse(report.is_move({"title": "Turn on invocation logging"}))
        self.assertFalse(report.is_move({"title": "Move logs", "kind": "improve"}), "the specialist's kind decides")

    def test_section_four_links_every_recommendation_and_folds_the_rest(self):
        sp = self.specialists()[1]
        html = report.rec_fields_html(1, sp["recommendations"][0], sp, {}, set())
        self.assertIn('id="rec-database-1"', html)
        self.assertIn('<details class="rec-more">', html)
        # The first sentence is its own text in the folded table too, so a translation of the compact view is reused.
        self.assertIn('<td>It is like this today.<span class="cont"> More detail follows.</span></td>', html)

    def test_no_specialist_reviews(self):
        html = report.top_recommendations_html([], {}, {})
        self.assertIn(report.NO_SPECIALISTS, html)
        self.assertNotIn('class="toprec"', html)
        glance = report.modernization_glance_html(recommend.bootstrap(), [])
        self.assertEqual(re.findall(r'id="glance-([a-z-]+)"', glance), list(recommend.DIRECTIONS) + list(recommend.FOUNDATIONS))
        self.assertEqual(glance.count('class="dir-tile"'), 7); self.assertEqual(glance.count('class="dir-tile small"'), 2)
        self.assertIn(report.NOT_REVIEWED_LABEL, glance)


class SummaryOrderTests(unittest.TestCase):
    def test_summary_order_tile_count_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root / "03-as-is")
            r = run_python(str(REVIEW), "--as-is", str(root / "03-as-is"), "--output", str(root / "04-review"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run_python(str(RECOMMEND), "--inventory", str(root / "03-as-is/inventory.json"), "--findings", str(root / "04-review/findings.json"),
                           "--workload", "fixture", "--output", str(root / "05-modernization"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            path = root / "05-modernization/pathways.json"
            doc = json.loads(path.read_text())
            # An older pathways.json: without the two added pathways, and with one candidate, one conditional direction and a
            # conditional foundation (which is not counted).
            doc["candidates"] = [c for c in doc["candidates"] if c["pathway"] not in recommend.ADDED_PATHWAYS]
            for c in doc["candidates"]:
                c["status"] = {"container": "candidate", "genai": "conditional", "modern-devops": "conditional"}.get(c["pathway"], c["status"])
            path.write_text(json.dumps(doc))
            run = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            html = (root / "06-assessment/assessment.html").read_text()
            summary = re.search(r'<section class="page" id="p-summary">(.*?)<nav class="pager">', html, re.S).group(1)
            marks = ['class="kpis"', 'id="modernization-glance"', 'id="top-modernization"', 'id="wa-review"', "Key improvements", "Where to start", 'id="how-produced"']
            positions = [summary.index(m) for m in marks]
            self.assertEqual(positions, sorted(positions), "tiles, glance, top recommendations, Well-Architected review, then how it was produced")
            self.assertNotIn("<h3", summary[summary.index('id="how-produced"') + 40:], "the usage block closes the summary")
            self.assertIn("2 of 7</div>", summary); self.assertIn("Modernization directions recommended", summary)
            self.assertNotIn("one line per area", html)
            self.assertIn(report.NO_SPECIALISTS, summary)
            self.assertEqual(len(re.findall(r'id="glance-', summary)), 9, "the old pathways.json still shows all nine tiles")
            self.assertIn("so no license-included engine or operating system was found", summary)
            ids = set(re.findall(r'\bid="([^"]+)"', html))
            self.assertEqual([h for h in re.findall(r'href="#([^"]+)"', html) if h not in ids], [], "every tile links to its detail")
            self.assertEqual(json.loads((root / "06-assessment/assessment-summary.json").read_text())["modernization_directions_recommended"], ["container", "genai"])
            md = (root / "06-assessment/assessment.md").read_text()
            heads = ["## 1. Executive summary", "Modernization directions recommended: 2 of 7", "### Modernization at a glance", "**Foundations**",
                     "### Top modernization recommendations", "### Well-Architected review", "#### Key improvements", "### How this report was produced", "## 2. AS-IS architecture"]
            positions = [md.index(h) for h in heads]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("| Managed services | Not assessed |", md); self.assertIn("| Open source | Not assessed |", md)


if __name__ == "__main__":
    unittest.main()
