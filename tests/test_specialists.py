"""Specialist SA Agent reviews (stage 5): briefs, validation, merge, synthesis check and the report section (offline fixtures)."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/aws-modernization-pathway/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run_python  # noqa: E402


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


advice = load("advice_under_test", "skills/aws-modernization-pathway/scripts/advice.py")
report = load("report_for_specialists", "skills/aws-modernization-pathway/scripts/assessment_report.py")
recommend = load("recommend_for_specialists", "skills/aws-modernization-pathway/scripts/recommend.py")

CLUSTER = "111111111111:us-east-1:rds.cluster:orders-db"
TASK = "111111111111:us-east-1:ecs.service:orders-api"


def build_run(root):
    snap = root / "03-as-is-enriched"
    snap.mkdir(parents=True)
    resources = [{"id": CLUSTER, "type": "rds.cluster", "name": "orders-db", "attributes": {}}, {"id": TASK, "type": "ecs.service", "name": "orders-api", "attributes": {}}]
    (snap / "inventory.json").write_text(json.dumps({"schema_version": "1.0", "account_id": "111111111111", "run_id": "20260101T000000Z", "regions": ["us-east-1"], "resources": resources}))
    (snap / "collection-manifest.json").write_text(json.dumps({"calls": [{"id": "e0001", "service": "rds", "operation": "describe-db-clusters"}]}))
    (snap / "scope-resources.json").write_text(json.dumps({"resources": [{"id": CLUSTER}, {"id": TASK}], "stack_resources": [{"StackName": "orders", "LogicalResourceId": "Pipeline"}]}))
    (root / "04-review").mkdir()
    checks = [{"id": "f0001", "rule_id": "AURORA_SINGLE_INSTANCE", "pillar": "Reliability", "resource_id": CLUSTER, "status": "FAIL", "severity": "HIGH", "evidence_ids": ["e0001"],
               "observation": "one writer", "recommendation": "add a reader"},
              {"id": "f0002", "rule_id": "CI_TEST_GATE", "pillar": "Operational Excellence", "resource_id": TASK, "status": "WARN", "severity": "MEDIUM", "evidence_ids": ["e0001"],
               "observation": "no test stage", "recommendation": "add tests"}]
    (root / "04-review/findings.json").write_text(json.dumps({"schema_version": "1.0", "account_id": "111111111111", "run_id": "20260101T000000Z",
                                                              "as_is_snapshot": {"path": "03-as-is-enriched"}, "checks": checks}))
    (root / "scope.json").write_text(json.dumps({"workload_name": "orders", "output_language": "en", "auth": {"profile": "assess"}}))
    (root / "05-modernization").mkdir()
    (root / "05-modernization/list-prices.json").write_text(json.dumps({"prices": [{"id": "p001", "label": "reader", "monthly_usd": 189.8, "basis": "1 x db.r6g.large"}]}))
    return root


def good_advice(sid="database", pathway="managed-database"):
    return {"schema_version": "1.0", "specialist": sid,
            "summary": "The order data sits on one writer. Raising the current cluster's availability comes before anything else.",
            "current_state": [{"text": "One writer in us-east-1b and no reader.", "refs": ["f0001", "e0001"]}],
            "recommendations": [{"title": "Give the order database a failover target", "pathway": pathway, "change": "Add a reader in us-east-1a to orders-db.",
                                 "benefit": "A writer failure promotes the reader instead of stopping the service.", "refs": ["f0001"], "effort": "days", "cost_direction": "up",
                                 "list_prices": [{"id": "db-p001", "sign": "+"}], "before_deciding": ["Agree RTO and RPO."],
                                 "alternatives": ["A smaller reader class when cost decides it."], "rollback": "Delete the reader."}],
            "not_now": [{"text": "No evidence supports another engine.", "refs": [CLUSTER]}],
            "handoffs": [{"to": "devops", "text": "Replica lag alarms belong to the alarm set."}],
            "diagram": {"svg": "diagram.aws.svg", "caption": "Data tier with the proposed reader."},
            "references": [{"title": "Aurora high availability", "url": "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html"}]}


def write_review(run, sid, adv, graph=None, svg_ids=None, candidates=None):
    folder = run / "05-modernization/specialists" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "advice.json").write_text(json.dumps(adv))
    pathways = advice.SPECIALISTS[sid]["pathways"]
    (folder / "candidates.json").write_text(json.dumps(candidates if candidates is not None else [c for c in recommend.bootstrap() if c["pathway"] in pathways]))
    graph = graph or {"nodes": [{"id": "n1", "label": "orders-db writer", "state": "changed", "resource_ids": [CLUSTER]}, {"id": "n2", "label": "reader", "state": "proposed"}],
                      "edges": [{"id": "x1", "source": "n1", "target": "n2", "label": "replication", "state": "proposed"}]}
    (folder / "diagram-graph.json").write_text(json.dumps(graph))
    ids = svg_ids if svg_ids is not None else [n["id"] for n in graph["nodes"]]
    (folder / "diagram.aws.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg">' + "".join(f'<g data-node-id="{i}"><text>{i}</text></g>' for i in ids) + "</svg>")
    (folder / "list-prices.json").write_text(json.dumps({"prices": [{"id": "db-p001", "label": "reader", "monthly_usd": 189.8, "basis": "1 x db.r6g.large"}]}))
    return folder


class SpecialistReviewTests(unittest.TestCase):
    def test_briefs_name_the_role_domain_resources_and_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            inventory = json.loads((run / "03-as-is-enriched/inventory.json").read_text())
            inventory["resources"][0]["attributes"] = {"Engine": "aurora-postgresql", "EngineVersion": "16.4", "MultiAZ": False, "BackupRetentionPeriod": 1, "DeletionProtection": False,
                                                       "DBClusterMembers": [{"DBInstanceIdentifier": "w", "IsClusterWriter": True}], "StorageEncrypted": True, "DBClusterArn": "arn:x"}
            (run / "03-as-is-enriched/inventory.json").write_text(json.dumps(inventory))
            (run / "04-review/diagrams").mkdir()
            (run / "04-review/diagrams/diagram-graph.json").write_text(json.dumps({"groups": [{"id": "vpc", "kind": "vpc", "label": "VPC", "parent": None}],
                                                                                  "nodes": [{"id": "n1", "label": "Aurora cluster orders-db", "type": "rds.cluster", "group": "vpc", "resource_ids": [CLUSTER]}], "edges": []}))
            (run / "05-modernization/candidates.draft.json").write_text(json.dumps([{"pathway": "managed-database", "status": "not_assessed"}, {"pathway": "container", "status": "not_assessed"}]))
            result = advice.brief(advice.Context(run), list(advice.ORDER))
            self.assertEqual([b["specialist"] for b in result], ["containers", "database", "analytics", "genai", "devops"])
            text = (run / "05-modernization/specialists/database/brief.md").read_text()
            self.assertIn("Database Specialist SA Agent", text); self.assertIn(CLUSTER, text); self.assertNotIn(TASK, text)
            self.assertIn("--id-prefix db-", text); self.assertIn("advice.py\" validate", text); self.assertIn("f0001", text)
            # The dossier is the whole input: configuration values, full finding text, diagram nodes, prices, drafts, rules, domain guide, skeleton.
            for expected in ("engine=aurora-postgresql 16.4", "writers=1", "readers=0", "backup_retention_days=1", "| f0001 | AURORA_SINGLE_INSTANCE | FAIL | HIGH |", "add a reader",
                             "| n1 | Aurora cluster orders-db |", "| p001 | reader |", '"pathway": "managed-database"', "This file is your whole input",
                             "# Shared rules for every Specialist SA Agent", "# Your domain guide", "## What to look at",
                             '"specialist": "database"', "diagram.py\" --graph"):
                self.assertIn(expected, text, expected)
            self.assertNotIn('"pathway": "container"', text, "only the specialist's own pathway drafts")
            self.assertNotIn("aws-diagram-design", text, "no redraw step")
            self.assertIn(TASK, (run / "05-modernization/specialists/containers/brief.md").read_text())

    def test_validate_accepts_grounded_advice_and_refuses_brochure_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            write_review(run, "database", good_advice())
            ctx = advice.Context(run)
            self.assertEqual(advice.validate(ctx, "database")["status"], "pass", advice.validate(ctx, "database")["errors"])
            bad = good_advice()
            bad["summary"] = "This seamless change cuts cost by 30% for $190 a month and is done within two weeks. 켜십시오."
            bad["recommendations"][0]["refs"] = ["f9999"]
            bad["recommendations"][0]["alternatives"] = []
            write_review(run, "database", bad)
            errors = " | ".join(advice.validate(advice.Context(run), "database")["errors"])
            for expected in ("promotional wording", "unmeasured effect percentage", "an amount", "delivery duration", "imperative", "refs must cite", "alternatives"):
                self.assertIn(expected, errors)
            self.assertEqual(advice.wording_errors({"current_state": [{"text": "27% of the tagged resources were created outside CloudFormation."}]}), [],
                             "an observed share is a fact, not a promised effect")
            self.assertEqual(advice.wording_errors({"summary": "Measure node use for two weeks, then size the group."}), [], "a measurement window is advice")
            self.assertIn("glossary replaces", " ".join(advice.wording_errors({"summary": "\uc54c\ub78c\uc744 \ucd94\uac00\ud569\ub2c8\ub2e4."})),
                          "a term the Korean glossary replaces is refused before the localizer sees it")
            # The optional `current` sentence follows the wording rules; `evidence` is benefits.py's, never the review's.
            own = good_advice()
            own["recommendations"][0]["current"] = "One writer in us-east-1b, no reader; a saving of $400 a month is available."
            own["recommendations"][0]["evidence"] = [{"metric": "deploys", "value": "3", "source": "me"}]
            write_review(run, "database", own)
            errors = " | ".join(advice.validate(advice.Context(run), "database")["errors"])
            self.assertIn("recommendations[0].current", errors); self.assertIn("written by benefits.py", errors)
            own["recommendations"][0]["current"] = "One writer in us-east-1b and no reader."
            del own["recommendations"][0]["evidence"]
            write_review(run, "database", own)
            self.assertEqual(advice.validate(advice.Context(run), "database")["status"], "pass")

    def test_validate_checks_the_diagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            write_review(run, "database", good_advice(), graph={"nodes": [{"id": "n1", "label": "writer", "state": "retained", "resource_ids": [CLUSTER]},
                                                                          {"id": "n2", "label": "reader", "state": "proposed", "resource_ids": [CLUSTER]}], "edges": []})
            errors = " | ".join(advice.validate(advice.Context(run), "database")["errors"])
            self.assertIn("proposed and cannot claim existing resource_ids", errors)
            write_review(run, "database", good_advice(), graph={"nodes": [{"id": "n1", "label": "writer", "state": "retained", "resource_ids": [CLUSTER]}], "edges": []})
            self.assertIn("shows no change", " | ".join(advice.validate(advice.Context(run), "database")["errors"]))
            write_review(run, "database", good_advice(), svg_ids=["n1"])
            self.assertIn("lacks data-node-id for n2", " | ".join(advice.validate(advice.Context(run), "database")["errors"]))
            folder = write_review(run, "database", good_advice())
            (folder / "diagram.aws.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
            self.assertEqual(advice.validate(advice.Context(run), "database")["status"], "fail")

    def test_merge_needs_every_review_folds_prices_and_check_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            write_review(run, "database", good_advice())
            ctx = advice.Context(run)
            self.assertEqual(advice.merge(ctx)["status"], "fail", "missing reviews are not merged silently")
            devops = good_advice("devops", "modern-devops")
            devops["recommendations"][0].update(title="Add a test stage", refs=["f0001", "f0002"], list_prices=[])
            devops["handoffs"] = []
            write_review(run, "devops", devops)
            result = advice.merge(advice.Context(run), allow_missing=True)
            self.assertEqual(result["status"], "merged")
            self.assertEqual(result["shared_references"], {"f0001": ["database", "devops"]})
            prices = json.loads((run / "05-modernization/list-prices.json").read_text())
            self.assertEqual([p["id"] for p in prices["prices"]], ["p001", "db-p001"])
            merged = json.loads((run / "05-modernization/specialist-advice.json").read_text())
            self.assertEqual([s["specialist"] for s in merged["specialists"]], ["database", "devops"])
            self.assertEqual(merged["specialists"][0]["diagram"]["svg"], "specialists/database/diagram.aws.svg")
            combined = json.loads((run / "05-modernization/candidates.specialists.json").read_text())
            self.assertEqual(len(combined), 7)
            # Synthesis: a changed status needs a reason.
            pathways = [dict(c) for c in combined]
            next(c for c in pathways if c["pathway"] == "managed-database")["status"] = "not_recommended"
            (run / "05-modernization/pathways.json").write_text(json.dumps({"candidates": pathways}))
            self.assertEqual(advice.check_synthesis(advice.Context(run))["status"], "fail")
            next(c for c in pathways if c["pathway"] == "managed-database")["synthesis_note"] = "The containers review moves the data path first."
            (run / "05-modernization/pathways.json").write_text(json.dumps({"candidates": pathways}))
            self.assertEqual(advice.check_synthesis(advice.Context(run))["status"], "pass")

    def test_report_section_renders_each_specialist_with_its_diagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            write_review(run, "database", good_advice())
            advice.merge(advice.Context(run), allow_missing=True)
            ctx = advice.Context(run)
            prices = report.load_list_prices(run)
            specialists = report.load_specialists(run, ctx.findings["checks"], [], ctx.manifest, ctx.inventory, prices)
            self.assertEqual(specialists[0]["recommendations"][0]["list_prices"][0]["monthly_usd"], 189.8)
            html = report.specialist_html(specialists[0], {"managed-database": "conditional"}, ctx.finding_ids, run)
            # Every recommendation is rendered in the same four fields; the evidence field holds only the cited list prices
            # (and later the figures benefits.py computes), never a number the specialist wrote.
            for text in ("Database Specialist SA Agent", "What we see now", "Not now", "Current state", "Proposal", "Quantitative evidence", "Prerequisites",
                         "What changes", "What it gives", "Before deciding", "How to undo", "AWS Price List API, On-Demand (db-p001)",
                         'id="zoom-sp-database"', "List price +$190/mo", 'href="#f0001"', 'href="#ev-e0001"'):
                self.assertIn(text, html)
            self.assertNotIn("<script", html)
            by_id = {c["id"]: c for c in ctx.findings["checks"]}
            html = report.specialist_html(specialists[0], {"managed-database": "conditional"}, ctx.finding_ids, run, by_id)
            self.assertIn(f'<th>Current state</th><td>{by_id["f0001"]["observation"]}</td>', html, "without a `current` sentence the cited finding's observation is shown")
            md = "\n".join(report.specialist_markdown(specialists[0], {"managed-database": "conditional"}, by_id))
            self.assertIn("### Database Specialist SA Agent (Managed database: Conditional)", md); self.assertIn("- Proposal:", md)
            self.assertIn("| + reader | $190/month |", md)
            rows = report.glance_rows("orders", [], ctx.findings["checks"], ctx.inventory, {}, None, {}, specialists)
            self.assertIn("Give the order database a failover target", " ".join(r[2] for r in rows))
            table = report.pathway_table([dict(c, problem="p", rationale="r") for c in recommend.bootstrap()], specialists)
            self.assertIn("Database Specialist SA Agent", table)
            # Invented references in the merged file stop the report like an invented key-issue reference does.
            doc = json.loads((run / "05-modernization/specialist-advice.json").read_text())
            doc["specialists"][0]["recommendations"][0]["refs"] = ["f4242"]
            (run / "05-modernization/specialist-advice.json").write_text(json.dumps(doc))
            with self.assertRaises(ValueError):
                report.load_specialists(run, ctx.findings["checks"], [], ctx.manifest, ctx.inventory, prices)

    def test_list_price_id_prefix_is_checked_before_any_call(self):
        # People are not named in Korean text either: the report masks "IAM <user> name" and every user name the evidence holds.
        self.assertEqual(report.mask_people("IAM \uc0ac\uc6a9\uc790 jdoe\uc5d0\uac8c"), "IAM \uc0ac\uc6a9\uc790 j***\uc5d0\uac8c")
        names = report.iam_user_names({"arn": "arn:aws:iam::111111111111:user/jdoe", "entry": "access-entry/c/user/111111111111/admin"})
        self.assertEqual(names, {"jdoe"}, "generic names such as admin are not treated as people")
        report.KNOWN_USERS.update(names)
        try:
            self.assertEqual(report.mask_people("granted to jdoe directly; SG public admin"), "granted to j*** directly; SG public admin")
        finally:
            report.KNOWN_USERS.clear()
        self.assertTrue(advice.people_errors({"summary": "cluster admin is bound to jdoe"}, {"jdoe"}))
        report.WALL_CLOCK["minutes"] = 79
        try:
            self.assertIn("1 h 19 min", report.run_clock_html({"run_id": "20200101T000000Z"}), "a re-render keeps the first wall-clock figure")
        finally:
            report.WALL_CLOCK["minutes"] = None
        with tempfile.TemporaryDirectory() as tmp:
            out = run_python(str(SCRIPTS / "list_price.py"), "--output", str(Path(tmp) / "p.json"), "--id-prefix", "DB_", "eks", "--support", "extended", "--label", "x",
                             capture_output=True, text=True)
            self.assertEqual(out.returncode, 1); self.assertIn("--id-prefix", out.stderr)


if __name__ == "__main__":
    unittest.main()
