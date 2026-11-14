"""Visual HTML reports: shared kit parity, chart safety and end-to-end structure (no AWS access)."""
import importlib.util
import json
from pathlib import Path
import re
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
locale = load("localize_artifacts", "skills/aws-mcp-setup/scripts/localize_artifacts.py")


class ReportKitTests(unittest.TestCase):
    def test_both_skill_copies_are_identical(self):
        a = (ROOT / "skills/aws-architecture-review/scripts/report_kit.py").read_bytes()
        b = (ROOT / "skills/aws-modernization-pathway/scripts/report_kit.py").read_bytes()
        self.assertEqual(a, b, "report_kit.py drifted between the two skills")

    def test_charts_are_accessible_static_svg(self):
        scores = {"Security": {"score": 54.2, "evidence_coverage_percent": 96.3, "counts": {"PASS": 14, "FAIL": 9, "WARN": 3, "UNKNOWN": 1}},
                  "Reliability": {"score": None, "evidence_coverage_percent": 0.0, "counts": {"UNKNOWN": 1, "NOT_APPLICABLE": 2}}}
        checks = [{"status": "FAIL", "severity": "HIGH"}, {"status": "WARN", "severity": "LOW"}, {"status": "PASS", "severity": "MEDIUM"}]
        charts = [kit.score_bars(scores), kit.stacked_status(scores), kit.donut({"FAIL": 9, "PASS": 14}),
                  kit.severity_bars(checks), kit.count_bars([("a<b>", 3), ("c", 1)], "c", "t", "d", suffix="%"),
                  kit.gantt([("W1", 0, 0, "Managed Database, Serverless, Modern DevOps and several more pathways"), ("W2", 1, 1, "Container"), ("W3", 2, 2, "Cloud Native"), ("W4", 3, 3, "Analytics")], focal="W1")]
        for markup in charts:
            elements = kit.scan_svg(markup)  # raises on scripts, handlers or external references
            root_tag, root = elements[0]
            self.assertEqual((root_tag, root["xmlns"]), ("svg", "http://www.w3.org/2000/svg"))
            self.assertEqual(root["role"], "img")
            self.assertIn(root["data-chart"] + "-title", root["aria-labelledby"])
            self.assertEqual(elements[1][0], "title")
        self.assertIn("Unassessed", charts[0])
        self.assertIn("&lt;b&gt;", charts[4])
        self.assertIn("\u2026", charts[5], "long wave notes are truncated with the full text kept in the bar title")
        self.assertEqual(kit.gantt([]), "")

    def test_heat_cell_and_chips_escape_and_mark_unknown(self):
        self.assertIn("repeating-linear-gradient", kit.heat_cell(None))
        self.assertIn(">5<", kit.heat_cell(5))
        self.assertIn("&lt;img&gt;", kit.chip("<img>", kit.INK))
        self.assertIn("Not applicable", kit.status_chip("NOT_APPLICABLE"))


class EndToEndVisualReportTests(unittest.TestCase):
    def run_reports(self, root):
        fixture(root / "03-as-is")
        review = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"),
                                 "--as-is", str(root / "03-as-is"), "--output", str(root / "04-review"), capture_output=True, text=True)
        self.assertEqual(review.returncode, 0, review.stderr)
        recommend = run_python(str(ROOT / "skills/aws-modernization-pathway/scripts/recommend.py"),
                                    "--inventory", str(root / "03-as-is/inventory.json"), "--findings", str(root / "04-review/findings.json"),
                                    "--workload", "fixture", "--output", str(root / "05-modernization"), capture_output=True, text=True)
        self.assertEqual(recommend.returncode, 0, recommend.stderr)
        return (root / "04-review/review.html").read_text(), (root / "05-modernization/recommendations.html").read_text()

    def test_reports_contain_charts_sections_and_no_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            review, recommend = self.run_reports(Path(temporary))
        for page in (review, recommend):
            self.assertNotIn("<script", page.lower())
            self.assertNotIn("http://fonts", page)
            self.assertIn('class="hero"', page)
            self.assertIn('class="kpi', page)
        self.assertGreaterEqual(len(re.findall(r'data-chart="', review)), 5)
        for anchor in ("glance", "architecture", "findings", "scope"):
            self.assertIn(f'id="{anchor}"', review)
        self.assertIn("Final AWS diagram required", review)
        self.assertIn("Operational Excellence", review)
        for anchor in ("summary", "pathways", "architecture", "options", "roadmap", "details", "deferred"):
            self.assertIn(f'id="{anchor}"', recommend)
        self.assertIn('data-chart="verified-weight"', recommend)
        self.assertIn("repeating-linear-gradient", recommend, "unknown criteria render as hatched heat-map cells")
        self.assertIn("No wave has been assigned", recommend)
        self.assertIn("Move to Modern DevOps", recommend)
        self.assertIn("Amazon Bedrock AgentCore", recommend)

    def test_reports_remain_localizable_and_render_diagrams_inline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review, recommend = self.run_reports(root)
            seen = []
            translated = locale.convert_text(review, ".html", lambda s: seen.append(s) or None, "ko")
            self.assertIn('lang="ko"', translated)
            self.assertIn("At a glance", seen)
            self.assertTrue(any("Local pillar scores" == s for s in seen), "chart titles are translatable units")
            self.assertFalse(any("rgba(" in s for s in seen), "CSS stays out of the translation plan")
            svg = root / "as-is.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>t</title><rect width="10" height="10"/></svg>')
            rerun = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"), "--as-is", str(root / "03-as-is"),
                                    "--output", str(root / "04-review"), "--diagram", str(svg), capture_output=True, text=True)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            page = (root / "04-review/review.html").read_text()
            self.assertIn("<figure><svg", page)
            self.assertNotIn("Final AWS diagram required", page)


if __name__ == "__main__":
    unittest.main()
