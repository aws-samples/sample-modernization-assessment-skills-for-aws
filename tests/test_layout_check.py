"""DOM layout check of rendered reports (render_artifact.mjs --check).

The browser part runs only where Playwright resolves: set MODA_PLAYWRIGHT_DIR to a folder whose node_modules holds
playwright (the helper resolves it from its working directory, as in a project folder)."""
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404 - list-form invocation of this repository's own helper
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/aws-mcp-setup/scripts/render_artifact.mjs"
PLAYWRIGHT_DIR = os.environ.get("MODA_PLAYWRIGHT_DIR")


def run_check(page, out):
    return subprocess.run(["node", str(HELPER), str(page), str(out), "--check"], cwd=PLAYWRIGHT_DIR, capture_output=True, text=True, timeout=180)  # nosec B603 B607


class LayoutCheckTests(unittest.TestCase):
    def test_helper_parses(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        r = subprocess.run(["node", "--check", str(HELPER)], capture_output=True, text=True)  # nosec B603 B607
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--check", HELPER.read_text())

    @unittest.skipUnless(PLAYWRIGHT_DIR and shutil.which("node"), "set MODA_PLAYWRIGHT_DIR to a folder that resolves playwright")
    def test_check_passes_a_clean_page_and_names_each_defect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "clean.html"
            clean.write_text('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width"></head><body>'
                             '<p id="a">Text</p><a href="#a">link</a><table style="display:block;overflow-x:auto;max-width:100%"><tr><td>' + "x" * 300 + '</td></tr></table></body></html>')
            r = run_check(clean, root / "qa-clean")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            record = json.loads((root / "qa-clean/layout-check.json").read_text())
            self.assertEqual(record["status"], "pass"); self.assertEqual(record["screenshots"], [])
            broken = root / "broken.html"
            broken.write_text('<!doctype html><html lang="ko"><head><meta name="viewport" content="width=device-width"></head><body>'
                              '<a href="#missing">link</a><div style="width:200px"><table style="width:900px"><tr><td>wide</td></tr></table></div>'
                              '<p>This sentence was left in English and is not translated for the reader of the page.</p>'
                              '<img alt="broken image" src="data:image/png;base64,AAAA"></body></html>')
            r = run_check(broken, root / "qa-broken")
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            record = json.loads((root / "qa-broken/layout-check.json").read_text())
            found = {i["check"] for w in record["widths"] for i in w["issues"]}
            for check in ("dangling_links", "wider_than_container", "untranslated_text", "image_not_rendered", "page_overflow_x"):
                self.assertIn(check, found)
            self.assertTrue((root / "qa-broken/page.png").is_file(), "a screenshot only when something failed")


if __name__ == "__main__":
    unittest.main()
