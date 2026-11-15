"""Unified assessment report, qualitative grades and Service Screener import (offline fixtures)."""
import importlib.util
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_workflow import fixture  # noqa: E402
from harness import run_python


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit = load("report_kit", "skills/aws-architecture-review/scripts/report_kit.py")
REVIEW = ROOT / "skills/aws-architecture-review/scripts/review.py"
RECOMMEND = ROOT / "skills/aws-modernization-pathway/scripts/recommend.py"
REPORT = ROOT / "skills/aws-modernization-pathway/scripts/assessment_report.py"
IMPORTER = ROOT / "skills/aws-architecture-review/scripts/import_service_screener.py"


def check(id_, pillar, status, severity="MEDIUM", rule="R"):
    return {"id": id_, "rule_id": rule, "pillar": pillar, "status": status, "severity": severity}


class GradeTests(unittest.TestCase):
    def test_rubric_is_deterministic_and_traceable(self):
        P = "Security"
        self.assertEqual(kit.grade_pillar([check("a", P, "FAIL", "HIGH")], P)[0], "critical")
        self.assertEqual(kit.grade_pillar([check("a", P, "FAIL", "LOW")], P)[0], "weak")
        self.assertEqual(kit.grade_pillar([check(str(i), P, "WARN") for i in range(3)], P)[0], "weak")
        self.assertEqual(kit.grade_pillar([check("a", P, "WARN"), check("b", P, "PASS")], P)[0], "watch")
        self.assertEqual(kit.grade_pillar([check("a", P, "UNKNOWN")], P)[0], "watch")
        self.assertEqual(kit.grade_pillar([check("a", P, "PASS"), check("b", P, "UNKNOWN")], P)[0], "watch")  # an open question keeps the pillar at Watch
        self.assertEqual(kit.grade_pillar([check("a", P, "NOT_APPLICABLE")], P)[0], "unassessed")
        self.assertEqual(kit.grade_pillar([], P)[0], "unassessed")
        key, info = kit.grade_pillar([check("a", P, "FAIL", "HIGH", "X"), check("b", P, "FAIL", "HIGH", "X"), check("c", P, "WARN", "LOW", "Y")], P)
        self.assertEqual((key, info["fails"], info["high_fails"], [c["rule_id"] for c in info["drivers"]]), ("critical", 2, 2, ["X", "Y"]))
        self.assertIn("first match wins", kit.grade_rubric_html())


class ScreenerImportTests(unittest.TestCase):
    def write_screener(self, root, account="111111111111", regions=("us-east-1",)):
        data = {"__metadata": {"accountId": account, "regions": list(regions)},
                "ec2": {"summary": {"SGOpen": {"^description": "<b>1</b> open group", "__links": ["<a href='https://docs.aws.amazon.com/x'>doc</a>"], "remediation_doc": "https://docs.aws.amazon.com/y"}},
                        "detail": {"us-east-1": {"sg-fixture": {"SGOpen": {"criticality": "H", "shortDesc": "Open SG", "__categoryMain": "S", "value": "0.0.0.0/0"}},
                                                 "sg-other": {"SGOpen": {"criticality": "L", "shortDesc": "Open SG", "__categoryMain": "O", "value": 1}}}}},
                "ta": {"ignored": True}}
        path = root / "api-full.json"
        path.write_text(json.dumps(data))
        return path

    def test_import_maps_and_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / "03-as-is")
            ok = run_python(str(IMPORTER), "--as-is", str(root / "03-as-is"), "--input", str(self.write_screener(root)), "--output", str(root / "up"), "--commit", "abc", capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            findings = json.loads((root / "up/screener-findings.json").read_text())["checks"]
            self.assertEqual(len(findings), 2)
            matched = next(f for f in findings if f["resource_id"])
            self.assertEqual((matched["rule_id"], matched["pillar"], matched["severity"], matched["status"], matched["source"]), ("SS::ec2::SGOpen", "Security", "HIGH", "FAIL", "service-screener"))
            self.assertTrue(matched["resource_id"].endswith("sg-fixture"))
            self.assertIn("https://docs.aws.amazon.com/x", matched["source_urls"])
            self.assertNotIn("<b>", matched["observation"])
            manifest = json.loads((root / "up/upstream-manifest.json").read_text())
            self.assertEqual((manifest["findings"], manifest["unmatched_resources"], manifest["commit"]), (2, 1, "abc"))
            self.assertEqual(json.loads(ok.stdout)["findings"], 2)
            bad = run_python(str(IMPORTER), "--as-is", str(root / "03-as-is"), "--input", str(self.write_screener(root, account="222222222222")), "--output", str(root / "up2"), capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("account mismatch", bad.stderr)
            wide = run_python(str(IMPORTER), "--as-is", str(root / "03-as-is"), "--input", str(self.write_screener(root, regions=("us-east-1", "eu-west-1"))), "--output", str(root / "up3"), capture_output=True, text=True)
            self.assertEqual(wide.returncode, 1)
            self.assertIn("outside the snapshot", wide.stderr)


class AccountPostureTests(unittest.TestCase):
    """The account-level table speaks in enablement states and suggestions, never in FAIL."""

    def test_account_section_shows_states_and_consider_cards(self):
        sys.path.insert(0, str(ROOT / "skills/aws-modernization-pathway/scripts"))
        report = load("assessment_report_posture", "skills/aws-modernization-pathway/scripts/assessment_report.py")
        checks = [
            {"id": "f0001", "rule_id": "SECURITYHUB_ENABLED", "pillar": "Security", "status": "WARN", "severity": "MEDIUM", "target": "Security Hub · us-east-1",
             "observation": "Security Hub is not enabled in us-east-1.", "recommendation": "Worth considering when several teams share the account.",
             "enablement": "not_enabled", "source_urls": ["https://docs.aws.amazon.com/securityhub/latest/userguide/securityhub-enable.html"]},
            {"id": "f0002", "rule_id": "GUARDDUTY_ENABLED", "pillar": "Security", "status": "PASS", "severity": "HIGH", "target": "GuardDuty · us-east-1",
             "observation": "GuardDuty detector enabled.", "recommendation": "Keep it.", "enablement": "enabled", "source_urls": []},
            {"id": "f0003", "rule_id": "INSPECTOR_ENABLED", "pillar": "Security", "status": "WARN", "severity": "MEDIUM", "target": "Amazon Inspector · us-east-1",
             "observation": "Inspector scanning on: ecr; off: ec2, lambda.", "recommendation": "Whether to extend scanning depends on what runs where.",
             "enablement": "partial", "source_urls": []},
            {"id": "f0004", "rule_id": "TRUSTED_ADVISOR_ACCESS", "pillar": "Operational Excellence", "status": "UNKNOWN", "severity": "MEDIUM", "target": "Trusted Advisor",
             "observation": "The support API answered 'subscription required'.", "recommendation": "Which plan?", "enablement": "not_in_plan", "source_urls": []},
            {"id": "f0005", "rule_id": "IAC_COVERAGE", "pillar": "Operational Excellence", "status": "WARN", "severity": "LOW", "target": "IaC coverage · us-east-1",
             "observation": "473 of 651 tagged resources carry stack tags (73%).", "recommendation": "Bring the rest under IaC.", "enablement": "observed", "source_urls": []},
        ]
        html = report.account_posture_html(checks, {"service_coverage": {}, "discovery": {}})
        for label in ("Not enabled", "Enabled", "Partially enabled", "Not in support plan", "Observed", "Consider", "Services worth considering",
                      "What it is", "What enabling adds", "For this workload", "not a pass or fail"):
            self.assertIn(label, html, label)
        self.assertNotIn("FAIL", html)
        self.assertNotIn("Fail ·", html)
        self.assertEqual(html.count('class="card consider"'), 2)  # Security Hub and Inspector; not IaC coverage, not the plan question
        self.assertIn("Amazon GuardDuty is a managed threat detection service", report.ACCOUNT_SERVICE_INFO["GUARDDUTY_ENABLED"][0])
        # findings from an older review without enablement states fall back to the judgement chip
        legacy = dict(checks[0]); legacy.pop("enablement")
        self.assertIn("Warn · WARN", report.account_posture_html([legacy], {}))


class ReaderFeedbackTests(unittest.TestCase):
    """Changes that came out of persona reviews of a live report (round 1): questions asked once, people not named,
    issues sized without invented numbers, recommendation strength visible, open questions keep a pillar at Watch."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "skills/aws-modernization-pathway/scripts"))
        self.report = load("assessment_report_feedback", "skills/aws-modernization-pathway/scripts/assessment_report.py")

    def test_identical_questions_are_asked_once(self):
        q = "May we run drift detection on this stack?"
        checks = [{"id": f"f00{i}", "rule_id": "STACK_DRIFT", "pillar": "Operational Excellence", "status": "UNKNOWN", "question": q, "recommendation": q,
                   "target": f"stack-{i}", "severity": "LOW"} for i in range(1, 8)]
        checks.append({"id": "f0099", "rule_id": "TRUSTED_ADVISOR_ACCESS", "pillar": "Operational Excellence", "status": "UNKNOWN", "question": "Which support plan?",
                       "recommendation": "Which support plan?", "target": "Trusted Advisor", "severity": "LOW"})
        groups = self.report.question_groups(checks)
        self.assertEqual(len(groups), 2); self.assertEqual(len(groups[0][1]), 7)
        grades = kit.grade_all(checks, self.report.PILLARS)
        html = self.report.pillar_sections(checks, grades, [])
        self.assertEqual(html.count(q), 1)
        self.assertIn("Open questions for this pillar</b> (2)", html)
        for c in checks:  # every original check keeps its anchors so finding and tile links still land
            self.assertIn(f'id="q-{c["id"]}"', html); self.assertIn(f'id="{c["id"]}"', html)
        self.assertNotIn('href="#questions"', html, "the standalone questions section is gone")

    def test_iam_user_names_are_masked_in_the_report_text(self):
        masked = self.report.mask_people("IAM user jdoe (long-lived credentials) has an access entry; arn:aws:iam::111111111111:user/jdoe-admin.")
        self.assertNotIn("jdoe", masked)
        self.assertIn("IAM user j***", masked); self.assertIn("user/j***", masked)
        self.assertEqual(self.report.mask_people("role/AppRole and user-data script"), "role/AppRole and user-data script")
        prose = "One IAM user with long-term keys has an EKS access entry."
        self.assertEqual(self.report.mask_people(prose), prose, "ordinary words after 'IAM user' are not names")

    def test_key_issue_size_vocabulary_and_quick_wins_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.json"
            base = {"title": "t", "problem": "p", "impact": "i", "action": "One. Two.", "urgency": "now", "finding_ids": ["f0001"]}
            path.write_text(json.dumps([dict(base, effort="soon")]))
            with self.assertRaises(ValueError):
                self.report.load_key_issues(path, {"f0001"})
            path.write_text(json.dumps([dict(base, title="Subscribe the alarm topic", effort="setting", cost_direction="neutral", owner="platform"),
                                        dict(base, title="Add a reader", effort="weeks", cost_direction="up", owner="platform"),
                                        dict(base, title="Unsized", urgency="later")]))
            issues = self.report.load_key_issues(path, {"f0001"})
        board = self.report.quick_wins_html(issues)
        self.assertIn("Can start this week", board); self.assertIn("Needs an investment decision", board)
        self.assertLess(board.index("Subscribe the alarm topic"), board.index("Add a reader"))
        self.assertNotIn("Unsized", board)
        cards = self.report.issues_html(issues)
        self.assertIn("Setting change", cards); self.assertIn("Cost up", cards); self.assertIn("Platform/DevOps setting", cards)
        self.assertIn('id="issue-1"', cards)
        self.assertEqual(self.report.quick_wins_html([issues[2]]), "", "no board when nothing is sized")

    def test_security_table_shows_how_firmly_each_item_is_recommended(self):
        checks = [{"id": "f0001", "rule_id": "IAM_WILDCARD_PERMISSION", "pillar": "Security", "status": "FAIL", "severity": "HIGH", "resource_id": None, "target": "role/App",
                   "observation": "s3:*", "recommendation": "Narrow the statement."},
                  {"id": "f0002", "rule_id": "S3_VERSIONING", "pillar": "Security", "status": "WARN", "severity": "LOW", "resource_id": None, "target": "bucket",
                   "observation": "off", "recommendation": "Versioning could be enabled."}]
        html = self.report.security_chapter(checks, [])
        self.assertIn("Strongly recommended", html); self.assertIn("Optional", html); self.assertIn("How firmly", html)

    def test_templated_questions_group_per_rule_and_identical_texts_merge_across_pillars(self):
        drift = [{"id": f"f01{i}", "rule_id": "STACK_DRIFT", "pillar": "Operational Excellence", "status": "UNKNOWN", "severity": "LOW", "target": f"shopnow-{n}",
                  "question": f"Stack shopnow-{n} has never been drift-checked. May we run drift detection on it?", "recommendation": "x"} for i, n in enumerate(("network", "ecs", "eks"))]
        plan = "Which AWS Support plan does this account have? The Trusted Advisor and Health APIs answered 'subscription required'."
        drift += [{"id": "f0100", "rule_id": "TRUSTED_ADVISOR_ACCESS", "pillar": "Operational Excellence", "status": "UNKNOWN", "severity": "LOW", "target": "Trusted Advisor", "question": plan, "recommendation": plan},
                  {"id": "f0101", "rule_id": "HEALTH_OPEN_EVENTS", "pillar": "Reliability", "status": "UNKNOWN", "severity": "LOW", "target": "AWS Health", "question": plan, "recommendation": plan}]
        groups = self.report.question_groups(drift)
        self.assertEqual([len(m) for _, m in groups], [3, 2])
        self.assertIn("the resources listed", groups[0][0])
        html = self.report.pillar_sections(drift, kit.grade_all(drift, self.report.PILLARS), [])
        self.assertEqual(html.count("drift detection"), 1)
        self.assertEqual(html.count("Which AWS Support plan"), 1, "a question shared by two pillars is listed once, in its first pillar")
        self.assertIn('id="f0101"', html)

    def test_masking_covers_eks_access_entries_and_grade_tiles_use_words(self):
        self.assertEqual(self.report.mask_people("access-entry/shopnow/user/111111111111/jdoe and arn:aws:iam::111111111111:user/jdoe"),
                         "access-entry/shopnow/user/111111111111/j*** and arn:aws:iam::111111111111:user/j***")
        self.assertEqual(self.report.mask_people("user-data script, role/user-service"), "user-data script, role/user-service")
        docs = "https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html"
        self.assertEqual(self.report.mask_people(docs), docs, "documentation URLs are not IAM users")
        self.report.KNOWN_USERS.add("jdoe")
        try:
            self.assertEqual(self.report.mask_people("Target user/jdoe (EKS access entry)"), "Target user/j*** (EKS access entry)",
                             "a principal written as user/<name> without the ARN prefix is masked too")
            self.assertEqual(self.report.mask_people(docs), docs, "a known name does not change documentation URLs")
        finally:
            self.report.KNOWN_USERS.discard("jdoe")
        self.assertEqual(self.report.rule_words("ALARM_NOTIFICATION_TARGET"), "Alarm notification target")
        self.assertEqual(self.report.rule_words("EKS_ADMIN_IAM_USER"), "EKS admin IAM user")
        self.assertEqual(self.report.rule_words("GENAI_INVOCATION_LOGGING"), "GenAI invocation logging")
        checks = [{"pillar": "Security", "status": "FAIL", "severity": "HIGH", "rule_id": "IAM_WILDCARD_PERMISSION"}]
        tiles = kit.grade_tiles(kit.grade_all(checks, ["Security"]), {"Security": "pillar-security"}, rule_labels=self.report.rule_label)
        self.assertIn("IAM wildcard permission", tiles); self.assertNotIn("IAM_WILDCARD_PERMISSION", tiles)

    def test_live_signals_box_lists_current_state_observations(self):
        checks = [{"id": "f0088", "rule_id": "LOG_DELIVERY", "pillar": "Operational Excellence", "status": "WARN", "severity": "MEDIUM", "target": "/shopnow/ecs/order-api",
                   "observation": "last event 5 days ago while the producer still runs.", "recommendation": "x"},
                  {"id": "f0001", "rule_id": "EBS_ENCRYPTION", "pillar": "Security", "status": "FAIL", "severity": "HIGH", "target": "vol-1", "observation": "off", "recommendation": "x"}]
        box = self.report.live_signals_html(checks)
        self.assertIn("Happening now", box); self.assertIn("order-api", box); self.assertNotIn("vol-1", box)
        self.assertEqual(self.report.live_signals_html([checks[1]]), "")

    def test_account_bill_table_renders_only_from_collected_figures(self):
        inventory = {"resources": [{"type": "ce.cost-by-service", "collection": {"status": "ok"}, "attributes": {"months": [
            {"start": "2026-08-01", "total_usd": 211.5, "services": [{"service": "Amazon OpenSearch Service", "usd": 153.6}, {"service": "Amazon Virtual Private Cloud", "usd": 57.9}]},
            {"start": "2026-09-01", "total_usd": 360.8, "services": [{"service": "Amazon OpenSearch Service", "usd": 221.8}, {"service": "Amazon Neptune", "usd": 139.0}]}]}}]}
        html = self.report.spend_html(inventory)
        self.assertIn("Account bill, last 2 closed months", html); self.assertIn("<th>2026-09</th>", html); self.assertIn("<td>361</td>", html)
        self.assertIn("Amazon Neptune", html); self.assertIn("not separated", html)
        self.assertEqual(self.report.spend_html({"resources": []}), "")
        self.assertEqual(self.report.spend_html({"resources": [{"type": "ce.cost-by-service", "collection": {"status": "not_enabled"}, "attributes": {"months": []}}]}), "")

    def test_scope_context_answers_questions_and_strengths_box_lists_passes(self):
        plan = "Which AWS Support plan does this account have?"
        checks = [{"id": "f0100", "rule_id": "TRUSTED_ADVISOR_ACCESS", "pillar": "Operational Excellence", "status": "UNKNOWN", "severity": "LOW", "target": "Trusted Advisor", "question": plan, "recommendation": plan},
                  {"id": "f0126", "rule_id": "REL_RESTORE_TEST", "pillar": "Reliability", "status": "UNKNOWN", "severity": "LOW", "target": "workload", "question": "What are the RTO and RPO?", "recommendation": "x"}]
        context = {"support_plan": "business", "environment": "production", "operator": "provider"}
        html = self.report.pillar_sections(checks, kit.grade_all(checks, self.report.PILLARS), [], context)
        self.assertNotIn("Support plan does", html); self.assertIn("RTO and RPO", html)
        self.assertIn('id="q-f0100"', html, "hidden questions keep their anchor")
        text = self.report.scope_context_text(context)
        self.assertIn("support plan: business", text); self.assertIn("operated by: provider", text)
        self.assertEqual(len(self.report.question_groups(checks, {"support_plan": "basic"})), 1)
        self.assertEqual(len(self.report.question_groups(checks, {})), 2)
        passed = [{"id": f"f00{i}", "rule_id": "EBS_ENCRYPTION", "pillar": "Security", "status": "PASS", "severity": "HIGH", "target": f"vol-{i}", "observation": "Encrypted=True", "recommendation": "keep"} for i in range(4)]
        passed.append({"id": "f0093", "rule_id": "GUARDDUTY_ENABLED", "pillar": "Security", "status": "PASS", "severity": "HIGH", "target": "GuardDuty", "observation": "on", "recommendation": "keep", "enablement": "enabled"})
        box = self.report.strengths_html(passed)
        self.assertIn("Already in good shape", box); self.assertIn("EBS encryption", box); self.assertIn("4 resource(s)", box); self.assertIn("GuardDuty", box)
        self.assertEqual(self.report.strengths_html([]), "")
        self.assertIn("provider", self.report.OWNER)

    def test_list_price_helper_picks_one_price_and_key_issues_cite_it(self):
        lp = load("list_price_fixture", "skills/aws-modernization-pathway/scripts/list_price.py")
        aurora = {"product": {"sku": "S1"}, "terms": {"OnDemand": {"t": {"priceDimensions": {
            "d1": {"unit": "Hrs", "description": "$0.26 per RDS db.r6g.large Single-AZ instance hour", "pricePerUnit": {"USD": "0.26"}},
            "d2": {"unit": "Hrs", "description": "$0.26 per RDS db.r6g.large Single-AZ unused capacity reservation hour", "pricePerUnit": {"USD": "0.26"}}}}}}}
        cache = {"product": {"sku": "S2"}, "terms": {"OnDemand": {"t": {"priceDimensions": {
            "d1": {"unit": "Hrs", "description": "$0.165 per Hr for ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.large", "pricePerUnit": {"USD": "0.165"}},
            "d2": {"unit": "Hrs", "description": "$0.206 per Memory optimized r6g.large node hour running Redis", "pricePerUnit": {"USD": "0.206"}}}}}}}
        self.assertEqual({p[0] for p in lp.hourly_prices([aurora], ("unused",))}, {0.26})
        self.assertEqual([p[0] for p in lp.hourly_prices([cache], ("ExtendedSupport",))], [0.206])
        self.assertEqual(len({p[0] for p in lp.hourly_prices([cache])}), 2, "ambiguous filters stay ambiguous; main() refuses them")
        with self.assertRaises(ValueError):
            lp.checked_arguments(["pricing", "get-products\n--x"])
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp); (run / "05-modernization").mkdir()
            (run / "05-modernization/list-prices.json").write_text(json.dumps({"prices": [
                {"id": "p001", "label": "Aurora reader", "monthly_usd": 189.8, "basis": "1 x Aurora PostgreSQL db.r6g.large, On-Demand, us-east-1; 730 hours a month"},
                {"id": "p002", "label": "current EKS nodes", "monthly_usd": 560.64, "basis": "2 x m5.2xlarge"},
                {"id": "p003", "label": "proposed EKS nodes", "monthly_usd": 147.17, "basis": "2 x m7i.large"}]}))
            prices = self.report.load_list_prices(run)
            path = run / "issues.json"
            base = {"title": "t", "problem": "p", "impact": "i", "action": "One. Two.", "urgency": "now", "finding_ids": ["f0001"]}
            path.write_text(json.dumps([dict(base, list_prices=[{"id": "p009", "sign": "+"}])]))
            with self.assertRaises(ValueError):
                self.report.load_key_issues(path, {"f0001"}, prices)
            path.write_text(json.dumps([dict(base, title="Add a reader", list_prices=[{"id": "p001", "sign": "+"}]),
                                        dict(base, title="Right-size nodes", list_prices=[{"id": "p002", "sign": "-"}, {"id": "p003", "sign": "+"}])]))
            issues = self.report.load_key_issues(path, {"f0001"}, prices)
        self.assertEqual(self.report.net_list_price(issues[0]), 189.8)
        self.assertEqual(self.report.net_list_price(issues[1]), -413.47)
        html = self.report.issues_html(issues)
        self.assertIn("List price +$190/mo", html); self.assertIn("List price −$413/mo", html)
        self.assertIn("not a bill forecast", html); self.assertIn("730 hours a month", html)

    def test_bill_table_marks_services_outside_the_workload_and_to_be_changes_table(self):
        inventory = {"resources": [{"id": "r1", "type": "rds.cluster"}, {"id": "r2", "type": "opensearch.domain"},
                                   {"type": "ce.cost-by-service", "collection": {"status": "ok"}, "attributes": {"months": [
                                       {"start": "2026-10-01", "total_usd": 743, "services": [{"service": "Amazon OpenSearch Service", "usd": 264}, {"service": "Amazon Relational Database Service", "usd": 120}]}]}}]}
        html = self.report.spend_html(inventory, scope_ids={"r1"})
        self.assertIn("<td>Amazon OpenSearch Service</td><td class=\"small\">no</td>", html)
        self.assertIn("<td>Amazon Relational Database Service</td><td class=\"small\">yes</td>", html)
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "to-be.aws.svg"; svg.write_text("<svg/>")
            (Path(tmp) / "diagram-graph.json").write_text(json.dumps({"nodes": [
                {"id": "a", "label": "Aurora reader (1a)", "state": "proposed", "detail": "failover target"},
                {"id": "b", "label": "EKS nodes", "state": "changed", "detail": "m5.2xlarge -> m7i.large"},
                {"id": "c", "label": "chatbot EC2", "state": "removal_candidate"}, {"id": "d", "label": "ALB", "state": "retained"}]}))
            table = self.report.to_be_changes_html(svg)
        self.assertIn("3 item(s); 1 component(s) kept", table); self.assertIn("Replace, then retire", table); self.assertIn("Aurora reader (1a)", table)
        self.assertEqual(self.report.to_be_changes_html(None), "")
        self.assertIn("Run wall-clock", self.report.run_clock_html({"run_id": "20260101T000000Z"}))
        self.assertEqual(self.report.run_clock_html({"run_id": "not-a-time"}), "")

    def test_round5_terms_examples_joins_and_environment_note(self):
        self.assertIn("Recovery time objective", self.report.terms_html("Agree an RTO with the owner."))
        self.assertNotIn("Karpenter", self.report.terms_html("Agree an RTO with the owner."))
        self.assertEqual(self.report.join_sentences(["Record the time.", "Take a snapshot"]), "Record the time. Take a snapshot.")
        self.assertEqual(self.report.join_sentences([]), "-")
        issue = {"cost_direction": "measure_first", "list_prices": [{"id": "p1", "sign": "-", "monthly_usd": 560.64, "label": "now", "basis": "b"}, {"id": "p2", "sign": "+", "monthly_usd": 119.14, "label": "example", "basis": "b"}]}
        self.assertIn("Example, before measurement: −$442/mo", self.report.issue_chips(issue))
        self.assertIn("not a saving", self.report.price_lines(issue))
        surcharge = {"cost_direction": "measure_first", "list_prices": [{"id": "p3", "sign": "+", "monthly_usd": 365.0, "label": "extended support", "basis": "b"}]}
        self.assertIn("List price +$365/mo", self.report.issue_chips(surcharge), "a cost of inaction is a list price, not an example")
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
        inventory = {"resources": [{"id": f"r{i}", "type": "rds.cluster", "attributes": {"ClusterCreateTime": recent}} for i in range(3)]}
        self.assertIn("5 days ago", self.report.environment_note(inventory, None, {"environment": "production"}))
        self.assertEqual(self.report.environment_note(inventory, None, {"environment": "unknown"}), "")
        old = {"resources": [{"id": f"r{i}", "type": "rds.cluster", "attributes": {"ClusterCreateTime": "2020-01-01T00:00:00Z"}} for i in range(3)]}
        self.assertEqual(self.report.environment_note(old, None, {"environment": "production"}), "")

    def test_round6_decision_board_security_ledger_compliance_and_plain_words(self):
        checks = [{"id": "f0118", "rule_id": "EKS_VERSION_SUPPORT", "pillar": "Reliability", "status": "WARN", "severity": "MEDIUM", "target": "shopnow",
                   "observation": "shopnow: Kubernetes 1.35 in standard support until 2027-03-27 (in 136 days).", "recommendation": "x"},
                  {"id": "f0001", "rule_id": "EBS_ENCRYPTION", "pillar": "Security", "status": "PASS", "severity": "HIGH", "target": "vol-1", "observation": "ok", "recommendation": "keep"},
                  {"id": "f0002", "rule_id": "IAM_WILDCARD_PERMISSION", "pillar": "Security", "status": "FAIL", "severity": "HIGH", "target": "role/App", "observation": "s3:*", "recommendation": "Narrow it."}]
        issues = [{"title": "Subscribe alarms", "effort": "setting", "cost_direction": "neutral", "list_prices": []},
                  {"title": "Add a reader", "effort": "days", "cost_direction": "up", "list_prices": [{"id": "p1", "sign": "+", "monthly_usd": 189.8, "label": "reader", "basis": "b"}]}]
        board = self.report.quick_wins_html(issues, checks)
        self.assertIn("Has a date", board); self.assertIn("2027-03-27", board)
        self.assertLess(board.index("Subscribe alarms"), board.index("Needs an investment decision"))
        self.assertGreater(board.index("Add a reader"), board.index("Needs an investment decision"), "a cost increase is an investment decision even when small")
        self.assertNotIn("net +$190/month", board, "no cross-issue total: costs of acting and of not acting do not add up")
        self.assertIn("List price +$190/mo", board)
        sec = self.report.security_chapter(checks, [])
        self.assertIn("In place today", sec); self.assertIn("EBS encryption", sec); self.assertIn("Needs a change", sec)
        card_checks = [{"id": "f0092", "rule_id": "SECURITYHUB_ENABLED", "pillar": "Security", "status": "WARN", "severity": "MEDIUM", "target": "Security Hub · us-east-1",
                        "observation": "off", "recommendation": "Worth considering.", "enablement": "not_enabled", "source_urls": []}]
        with_obligations = self.report.account_posture_html(card_checks, {}, None, {"compliance": ["ISMS-P", "PCI DSS"]})
        self.assertIn("Recommended for compliance", with_obligations); self.assertIn("ISMS-P, PCI DSS", with_obligations)
        self.assertNotIn("Recommended for compliance", self.report.account_posture_html(card_checks, {}, None, {"compliance": ["unknown"]}))
        self.assertEqual(self.report.plain_words("declared in the template (template_declared); see scope.json"), "declared in the template (from the template); see the scope interview")

    def test_round7_first_steps_usage_honesty_and_tags(self):
        issues = [{"title": "No alerting path", "effort": "days", "cost_direction": "up", "list_prices": [],
                   "first_step": {"text": "Subscribe the on-call address to shopnow-alerts", "effort": "setting"}}]
        board = self.report.quick_wins_html(issues, [])
        week = board[:board.index("Needs an investment decision")]
        self.assertIn("Subscribe the on-call address", week); self.assertNotIn("none declared", week)
        empty = self.report.quick_wins_html([{"title": "Big", "effort": "weeks", "cost_direction": "neutral", "list_prices": []}], [])
        self.assertIn("none declared", empty[:empty.index("Needs an investment decision")], "template lines no longer fill the list (no IaC section)")
        meta = {"totals": {"inference_seconds": 396}, "models_seen": ["claude-opus-5.5"], "entries": [], "notes": []}
        self.assertIn("describe only the first minutes", self.report.usage_html(meta, {"run_id": "20260101T000000Z"}))
        self.assertEqual(self.report.resource_tags({"attributes": {"Tags": [{"Key": "Project", "Value": "moda-testbed"}]}}), {"Project": "moda-testbed"})
        from datetime import datetime, timedelta, timezone
        old = "2020-01-01T00:00:00Z"
        inventory = {"resources": [{"id": f"r{i}", "type": "rds.cluster", "attributes": {"ClusterCreateTime": old, "TagList": [{"Key": "Project", "Value": "moda-testbed"}]}} for i in range(3)]}
        note = self.report.environment_note(inventory, None, {"environment": "production"})
        self.assertIn("Project=moda-testbed (3)", note)
        ns = load("ns_negation", "skills/aws-modernization-pathway/scripts/next_steps.py")
        self.assertFalse(ns.mentioned("no asynchronous flow was found.", "asynchronous"))
        self.assertTrue(ns.mentioned("an asynchronous order flow would decouple checkout.", "asynchronous"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "i.json"
            path.write_text(json.dumps([{"title": "t", "problem": "p", "impact": "i", "action": "One. Two.", "urgency": "now", "finding_ids": ["f1"], "first_step": {"text": "x", "effort": "weeks"}}]))
            with self.assertRaises(ValueError):
                self.report.load_key_issues(path, {"f1"}, {})

    def test_open_questions_keep_a_pillar_at_watch(self):
        checks = [{"pillar": "Cost Optimization", "status": "PASS", "severity": "LOW", "rule_id": "A"} for _ in range(13)]
        self.assertEqual(kit.grade_pillar(checks, "Cost Optimization")[0], "sound")
        checks.append({"pillar": "Cost Optimization", "status": "UNKNOWN", "severity": "LOW", "rule_id": "COST_WORKLOAD"})
        self.assertEqual(kit.grade_pillar(checks, "Cost Optimization")[0], "watch")

    def test_round8_summary_usage_reasons_pathways_and_zoom(self):
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
        inventory = {"account_id": "111111111111", "regions": ["us-east-1"], "resources": [
            {"id": f"r{i}", "type": "rds.cluster", "attributes": {"ClusterCreateTime": recent}} for i in range(3)] + [
            {"id": "bill", "type": "ce.cost-by-service", "level": "account", "collection": {"status": "ok"}, "attributes": {"months": [
                {"start": "2026-10-01", "total_usd": 743, "services": [{"service": "Amazon OpenSearch Service", "usd": 264}, {"service": "Amazon Relational Database Service", "usd": 120}]}]}}]}
        scope = {"workload": {"type": "stack", "stack_names": ["a", "b"]}}
        issues = [{"title": "Add a reader", "urgency": "now", "effort": "days", "cost_direction": "up", "list_prices": [{"id": "p1", "sign": "+", "monthly_usd": 189.8, "label": "r", "basis": "b"}]},
                  {"title": "Right-size nodes", "urgency": "later", "effort": "weeks", "cost_direction": "measure_first",
                   "list_prices": [{"id": "p2", "sign": "-", "monthly_usd": 560.6, "label": "now", "basis": "b"}, {"id": "p3", "sign": "+", "monthly_usd": 119.1, "label": "ex", "basis": "b"}],
                   "first_step": {"text": "Turn on Container Insights", "effort": "setting"}}]
        checks = [{"id": "f0001", "rule_id": "EBS_ENCRYPTION", "pillar": "Security", "status": "PASS", "severity": "HIGH", "target": "vol-1", "observation": "ok", "recommendation": "keep"}]
        rows = self.report.glance_rows("shopnow", issues, checks, inventory, scope, {"r0", "r1", "r2"}, {"environment": "production", "support_plan": "basic"})
        text = " ".join(md for _, _, md in rows)
        self.assertIn("account 111111111111 (us-east-1): 2 CloudFormation stack(s), 3 resources assessed", text)
        self.assertIn("$743 for every workload in the account; the services this workload uses came to at most $120", text)
        self.assertIn("support plan: basic", text)
        self.assertIn("5 days old", text, "the environment to confirm comes first")
        self.assertIn("List price +$190/month", text); self.assertIn("size decided after measurement", text)
        self.assertNotIn("442", text, "example prices before measurement stay out of the summary")
        self.assertIn("Turn on Container Insights", text); self.assertIn("EBS encryption", text)
        self.assertIn('id="glance"', self.report.glance_html(rows)); self.assertEqual(self.report.glance_html([]), "")
        self.assertIn("Services this workload uses:</b> $120 of the $743", self.report.spend_html(inventory, {"r0"}))
        meta = {"totals": {"inference_seconds": 338}, "models_seen": ["claude-opus-5.5"], "entries": [], "notes": [], "partial_fields": [], "agent_client": "kiro-cli", "model_id": None}
        md = "\n".join(self.report.usage_markdown(meta, {"run_id": "20260101T000000Z"}))
        self.assertIn("not shown as the run's usage", md); self.assertNotIn("5m 38s", md); self.assertIn("Run wall-clock", md)
        self.assertEqual(self.report.plain_words("over 0.0.0.0/0 (configuration, s0032) and (template_declared)"), "over 0.0.0.0/0 (API configuration, s0032) and (from the template)")
        pathways = [{"pathway": "container", "status": "conditional", "rationale": "Fits ECS.", "problem": "p", "target_services": ["Amazon ECS"], "finding_ids": ["f0001"], "wave": 2, "prerequisites": []},
                    {"pathway": "managed-analytics", "status": "not_assessed", "rationale": "Order data exists.", "problem": "p", "target_services": [], "finding_ids": [], "wave": None,
                     "prerequisites": ["Decide what the data would inform."]}]
        cards = self.report.pathway_cards(pathways)
        self.assertEqual(cards.count('class="pathcard"'), 1, "only recommended directions get a card")
        self.assertIn("Not recommended now or not assessed yet (1)", cards); self.assertIn("Decide what the data would inform.", cards)
        self.assertNotIn("wave 2", cards, "the order of work is left to the meeting")
        self.assertIn("Compliance obligations", self.report.compliance_callout({"compliance": ["unknown"]}, ["us-east-1"]))
        self.assertEqual(self.report.compliance_callout({"compliance": ["ISMS-P"]}, ["us-east-1"]), "")
        self.assertEqual(self.report.compliance_callout({}, ["us-east-1"]), "")
        figure = self.report.zoom_figure("zoom-as-is", "<svg></svg>", "current", "caption")
        self.assertIn('href="#zoom-as-is"', figure); self.assertIn('href="#current"', figure); self.assertNotIn("<script", figure)
        self.assertIn("figure.zoomable:target", kit.CSS if hasattr(kit, "CSS") else kit.page("t", ""))
        tiles = kit.grade_tiles(kit.grade_all([{"pillar": "Security", "status": "FAIL", "severity": "HIGH", "rule_id": "X"}] + checks, ["Security"]))
        self.assertIn("Set by 1 high-severity failure(s); 1 check(s) passed.", tiles)
        ns = load("ns_round8", "skills/aws-modernization-pathway/scripts/next_steps.py")
        reason = ns._merge_reasons(["resources ec2.instance; assessment mentions container, ecs", "resources ec2.instance; assessment mentions eks"])
        self.assertEqual(reason, "resources ec2.instance; assessment mentions container, ecs, eks")
        self.assertIsNone(self.report.environment_facts({"resources": []}, None, {"environment": "production"}))


class UnifiedReportTests(unittest.TestCase):
    def build_run(self, root):
        fixture(root / "03-as-is")
        r = run_python(str(REVIEW), "--as-is", str(root / "03-as-is"), "--output", str(root / "04-review"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_python(str(RECOMMEND), "--inventory", str(root / "03-as-is/inventory.json"), "--findings", str(root / "04-review/findings.json"), "--workload", "fixture", "--output", str(root / "05-modernization"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((root / "04-review/findings.json").read_text())["checks"]

    def test_report_renders_sections_grades_and_refuses_invented_issue_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checks = self.build_run(root)
            fail = next(c for c in checks if c["status"] == "FAIL")
            issues = root / "key-issues.json"
            issues.write_text(json.dumps({"issues": [{"title": "Admin ports open to the internet", "problem": "p", "impact": "i", "action": "Close the port.", "urgency": "now", "finding_ids": [fail["id"]]}]}))
            terse = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--key-issues", str(issues), "--output", str(root / "06-terse"), capture_output=True, text=True)
            self.assertEqual(terse.returncode, 1)
            self.assertIn("at least two sentences", terse.stderr)  # a one-line order is not a suggestion
            issues.write_text(json.dumps({"issues": [{"title": "Admin ports open to the internet", "problem": "p", "impact": "i", "action": 'Spreading the database over two Availability Zones would keep it serving if one zone fails. If cost is the reason for a single instance, right-size it first with Compute Optimizer, then add the replica.', "urgency": "now", "finding_ids": [fail["id"]]}]}))
            run = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--key-issues", str(issues), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            out = json.loads(run.stdout)
            self.assertEqual(out["report_status"], "Draft - diagrams pending")
            html = (root / "06-assessment/assessment.html").read_text()
            for anchor in ("issues", "current", "grades", "findings", "security", "directions", "next", "evidence", "appendix"):
                self.assertIn(f'id="{anchor}"', html)
            for gone in ("roadmap", "changes", "questions"):  # left to the meeting with the owner, or not every customer uses IaC
                self.assertNotIn(f'id="{gone}"', html)
            self.assertIn("<h2>Reference material</h2>", html)
            self.assertIn("Admin ports open to the internet", html)
            self.assertIn("1 · Decide first", html); self.assertNotIn("Act now", html)  # priority labels invite discussion
            self.assertIn("<b>Suggestion</b>", html); self.assertNotIn("<b>Next step</b>", html)
            self.assertIn('class="grade-tile"', html)
            self.assertIn("first match wins", html)
            self.assertNotIn("<script", html.lower())
            summary = json.loads((root / "06-assessment/assessment-summary.json").read_text())
            self.assertEqual(summary["grades"]["Security"]["grade"], "critical")
            self.assertEqual(summary["key_issues"][0]["finding_ids"], [fail["id"]])
            md = (root / "06-assessment/assessment.md").read_text()
            self.assertIn("## Key issues", md)
            self.assertIn("| Security | Fix first |", md)
            issues.write_text(json.dumps({"issues": [{"title": "x", "problem": "p", "impact": "i", "action": 'Spreading the database over two Availability Zones would keep it serving if one zone fails. If cost is the reason for a single instance, right-size it first with Compute Optimizer, then add the replica.', "urgency": "now", "finding_ids": ["f9999"]}]}))
            bad = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--key-issues", str(issues), "--output", str(root / "06-b"), capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("existing finding IDs", bad.stderr)
            none = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--output", str(root / "06-c"), capture_output=True, text=True)
            self.assertEqual(none.returncode, 0, none.stderr)
            self.assertIn("Key issues were not authored", (root / "06-c/assessment.html").read_text())

    def test_report_reads_the_snapshot_the_review_judged(self):
        """Field case 2026-11-03: the unified report counted resources from the base snapshot while the
        review had judged the host-merged one, so the two disagreed. review.py now records the snapshot
        it used and assessment_report.py must follow that pointer, not a hard-coded directory."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / "03-as-is")
            # A later, richer snapshot (as merge_host_evidence would produce) with one more resource.
            inventory = json.loads((root / "03-as-is/inventory.json").read_text())
            shutil.copytree(root / "03-as-is", root / "03-as-is-with-host")
            extra = dict(inventory["resources"][0], id="111111111111:us-east-1:ec2.security-group:sg-extra", native_id="sg-extra")
            inventory["resources"].append(extra)
            (root / "03-as-is-with-host/inventory.json").write_text(json.dumps(inventory))
            r = run_python(str(REVIEW), "--as-is", str(root / "03-as-is-with-host"), "--output", str(root / "04-review"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            findings = json.loads((root / "04-review/findings.json").read_text())
            self.assertEqual(findings["as_is_snapshot"]["path"], "03-as-is-with-host")
            self.assertEqual(findings["as_is_snapshot"]["resources"], 2)
            r = run_python(str(RECOMMEND), "--inventory", str(root / "03-as-is-with-host/inventory.json"), "--findings", str(root / "04-review/findings.json"), "--workload", "fixture", "--output", str(root / "05-modernization"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            run = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            html = (root / "06-assessment/assessment.html").read_text()
            self.assertIn("2 resources", html)
            self.assertNotIn("1 resources", html)
            # A dangling pointer must fail loudly instead of silently reading another snapshot.
            findings["as_is_snapshot"]["path"] = "03-as-is-missing"
            (root / "04-review/findings.json").write_text(json.dumps(findings))
            bad = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--output", str(root / "06-b"), capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("03-as-is-missing", bad.stderr)

    def test_report_includes_screener_chapter_side_by_side(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.build_run(root)
            screener = ScreenerImportTests()
            path = screener.write_screener(root)
            imp = run_python(str(IMPORTER), "--as-is", str(root / "03-as-is"), "--input", str(path), "--output", str(root / "04-review/upstream"), capture_output=True, text=True)
            self.assertEqual(imp.returncode, 0, imp.stderr)
            run = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--screener", str(root / "04-review/upstream/screener-findings.json"), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            html = (root / "06-assessment/assessment.html").read_text()
            self.assertIn("SGOpen", html, "imported checks appear as ordinary security items")
            self.assertNotIn("Service Screener", html, "the customer report does not name the scanner")
            self.assertNotIn("Screener", (root / "06-assessment/assessment.md").read_text())
            self.assertEqual(json.loads(run.stdout)["imported_findings"], 2)
            summary = json.loads((root / "06-assessment/assessment-summary.json").read_text())
            self.assertEqual(summary["imported_findings"]["tool"], "Service Screener", "provenance stays in the artifact")
            other = root / "04-review/upstream/screener-findings.json"
            doc = json.loads(other.read_text()); doc["account_id"] = "333333333333"; other.write_text(json.dumps(doc))
            bad = run_python(str(REPORT), "--run-dir", str(root), "--workload", "fixture", "--screener", str(other), "--output", str(root / "06-x"), capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)


if __name__ == "__main__":
    unittest.main()
