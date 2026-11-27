import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from test_workflow import load

locale = load("locale_artifacts", "skills/aws-mcp-setup/scripts/localize_artifacts.py")
kit = load("report_kit_locale", "skills/aws-modernization-pathway/scripts/report_kit.py")
KO = {
    "Assessment": "\ud3c9\uac00",
    "Found 2 issues": "2\uac1c \ubb38\uc81c\ub97c \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4",
    "Details": "\uc138\ubd80\uc0ac\ud56d",
    "Architecture": "\uc544\ud0a4\ud14d\ucc98",
    "Service": "\uc11c\ube44\uc2a4",
    "See": "\ucc38\uc870",
    "Pillar": "\uc601\uc5ed",
    "Score": "\uc810\uc218",
    "Security": "\ubcf4\uc548",
    "Review encryption": "\uc554\ud638\ud654 \uac80\ud1a0\ub97c \uad8c\uc7a5\ud569\ub2c8\ub2e4",
}


def bundle(root):
    root.mkdir()
    (root/"raw").mkdir()
    svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100"><title>Architecture</title><g data-node-id="node1"><text x="20" y="50">Service</text></g></svg>'
    embedded=base64.b64encode(svg.encode()).decode()
    (root/"report.html").write_text(f'''<!doctype html><html lang="en"><head><title>Assessment</title>
<style>body{{font:16px Arial}}code{{color:red}}</style></head><body><h1>Assessment</h1>
<p>Found 2 issues</p><p>f0001 EBS_ENCRYPTION FAIL</p>
<a href="details.md" title="Details">Details</a><img src="diagram.png" alt="Architecture">
<img src="data:image/svg+xml;base64,{embedded}" alt="Architecture">
<pre><code>print("Keep English")</code></pre><script type="application/json">{{"raw":"Keep English"}}</script>
</body></html>''')
    (root/"details.md").write_text('''# Assessment

Found 2 issues

See [Details](report.html).

`f0001` `EBS_ENCRYPTION` `FAIL`

```bash
echo "Keep English"
```

| Pillar | Score |
|---|---:|
| Security | 81.8 |
''')
    (root/"diagram.svg").write_text(svg)
    (root/"diagram.png").write_bytes(b"stale preview")
    (root/"report-preview.png").write_bytes(b"stale report preview")
    (root/"brand.png").write_bytes(b"unchanged bitmap asset")
    (root/"raw/e0001.json").write_text('{"observation":"Keep raw evidence","value":2}')
    (root/"findings.json").write_text(json.dumps({"checks":[{
        "id":"f0001","rule_id":"EBS_ENCRYPTION","status":"FAIL","weight":2,
        "resource_id":"111111111111:us-east-1:ec2.volume:vol-0123456789abcdef0",
        "evidence_ids":["e0001"],"observation":"Found 2 issues","recommendation":"Review encryption"
    }]}))
    (root/"scorecard.json").write_text('{"Security":{"score":81.8}}')
    (root/"qa-summary.json").write_text('{"status":"pass"}')


def translated_plan(root, language="ko"):
    plan=locale.prepare(root,locale.resolve_language(request=language))
    for unit in plan["units"].values():
        if unit["source"] in KO:
            unit["target"]=KO[unit["source"]]
        else:
            # The only remaining prose unit consists entirely of stable codes.
            if unit["source"]!="f0001 EBS_ENCRYPTION FAIL":
                raise AssertionError(unit["source"])
            unit["preserve"]=True
    return plan


class LocalizationTests(unittest.TestCase):
    def test_language_precedence_and_standalone_fallback(self):
        self.assertEqual(locale.resolve_language("ja","ko",{"output_language":"de"})["output_language"],"ja")
        self.assertEqual(locale.resolve_language(request="KO-kr")["output_language"],"ko-kr")
        self.assertEqual(locale.resolve_language(inherited={"output_language":"fr"})["language_source"],"inherited")
        self.assertEqual(locale.resolve_language()["language_source"],"standalone_cli_fallback")
        for bad in ("../ko","ko;execute","auto!","<html>"):
            with self.subTest(bad=bad),self.assertRaises(ValueError):locale.language_tag(bad)

    def test_missing_translation_fails_before_output_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"source";bundle(root);out=Path(d)/"ko"
            plan=locale.prepare(root,locale.resolve_language(request="ko"))
            with self.assertRaisesRegex(ValueError,"every prose unit"):locale.apply(plan,out)
            self.assertFalse(out.exists())

    def test_localized_assessment_has_one_fixed_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "06-assessment"; src.mkdir()
            (src / "assessment.md").write_text("# Title\n\nA sentence to translate.\n")
            plan = locale.prepare(src, {"output_language": "ko", "language_source": "explicit"})
            for unit in plan["units"].values():
                unit["target"] = "번역"
            with self.assertRaises(ValueError):
                locale.apply(plan, Path(tmp) / "06-assessment.ko")
            result = locale.apply(plan, Path(tmp) / "06-assessment-ko")
            self.assertTrue((Path(tmp) / "06-assessment-ko/assessment.md").is_file(), result)

    def test_localizes_prose_embedded_svg_and_json_without_changing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"source";bundle(root);out=Path(d)/"ko"
            original={str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result=locale.apply(translated_plan(root),out)
            page=(out/"report.html").read_text()
            self.assertIn('lang="ko"',page)
            self.assertIn(KO["Found 2 issues"],page)
            self.assertIn('href="details.md"',page)
            self.assertIn('print("Keep English")',page)
            self.assertIn('{"raw":"Keep English"}',page)
            encoded=page.split("data:image/svg+xml;base64,",1)[1].split('"',1)[0]
            embedded=base64.b64decode(encoded).decode()
            self.assertIn(KO["Service"],embedded)
            self.assertEqual(kit.scan_svg((out/"diagram.svg").read_text())[0][1]["viewbox"],"0 0 400 100")
            self.assertIn('data-node-id="node1"',embedded)
            md=(out/"details.md").read_text()
            self.assertIn(f'[{KO["Details"]}](report.html)',md)
            self.assertIn('echo "Keep English"',md)
            self.assertIn('`EBS_ENCRYPTION`',md)
            before=json.loads((root/"findings.json").read_text())
            after=json.loads((out/"findings.json").read_text())
            for key in ("id","rule_id","status","weight","resource_id","evidence_ids"):
                self.assertEqual(before["checks"][0][key],after["checks"][0][key])
            self.assertEqual(after["checks"][0]["observation"],KO["Found 2 issues"])
            for name in ("raw/e0001.json","scorecard.json","brand.png"):
                self.assertEqual((out/name).read_bytes(),original[name])
            self.assertEqual({str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()},original)
            self.assertFalse((out/"diagram.png").exists())
            self.assertEqual(set(result["rasters_to_regenerate"]),{"diagram.png","report-preview.png"})
            self.assertEqual(json.loads((out/"qa-summary.json").read_text())["status"],"requires_localization_qa")
            self.assertEqual(result["status"],"requires_visual_qa")

    def test_changed_protected_tokens_or_new_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"source";bundle(root)
            for target in ("Found 3 issues","Found 2 issues https://example.invalid"):
                plan=translated_plan(root)
                plan["units"][locale.unit_id("Found 2 issues")]["target"]=target
                with self.subTest(target=target),self.assertRaisesRegex(ValueError,"protected"):
                    locale.apply(plan,Path(d)/"out")
            source="Account 111111111111 in us-east-1 has f0001 EBS_ENCRYPTION FAIL"
            for target in (source.replace("us-east-1","us-west-2"),source.replace("f0001","f0002")):
                self.assertNotEqual(locale.PROTECTED.findall(source),locale.PROTECTED.findall(target))
            self.assertNotEqual(locale.PROTECTED.findall("Rate 50%"),locale.PROTECTED.findall("Rate 50"))
            self.assertNotEqual(locale.PROTECTED.findall("Value -1"),locale.PROTECTED.findall("Value 1"))
            # Status values are protected as values; the same words inside a sentence are ordinary words.
            self.assertFalse(locale.protected_match("conditional", "조건부 권장"))
            self.assertTrue(locale.protected_match("0 of 1 topic(s) with a confirmed subscriber", "확인된 구독자가 있는 토픽 0개(전체 1개)"))
            self.assertFalse(locale.protected_match("status FAIL", "상태 미충족"))
            # A month written as a word may become a number; any other new number is still refused.
            self.assertTrue(locale.protected_match("generally available Mar 2026", "2026년 3월 정식 출시"))
            self.assertFalse(locale.protected_match("generally available in 2026", "2026년 3월 정식 출시"))
            # A time-zone offset or a sign is not a "+" separator between nouns.
            self.assertEqual(locale.glossary_violations("2026-11-23T07:33:26+00:00까지 쓰기가 없었습니다.", {"+": "x"}, allow_bullets=False), [])
            self.assertEqual(locale.glossary_violations("경보 + 대시보드를 추가합니다.", {"+": "x"}, allow_bullets=False), ["+"])

    def test_source_changes_and_skill_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"source";bundle(root)
            plan=translated_plan(root)
            (root/"raw/e0001.json").write_text("{}")
            with self.assertRaisesRegex(ValueError,"source artifacts changed"):locale.apply(plan,Path(d)/"ko")
            (root/"SKILL.md").write_text("# English definition")
            with self.assertRaisesRegex(ValueError,"not skill definitions"):locale.prepare(root,locale.resolve_language())

    def test_english_copy_and_fresh_locale_source_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"source";bundle(root)
            plan=locale.prepare(root,locale.resolve_language("en-US"))
            out=Path(d)/"en"
            locale.apply(plan,out)
            self.assertIn('lang="en-us"',(out/"report.html").read_text())
            with self.assertRaisesRegex(ValueError,"canonical bundle"):locale.prepare(out,locale.resolve_language(request="ko"))
            with self.assertRaisesRegex(ValueError,"fresh output"):locale.apply(plan,root/"nested")

    def test_entities_markup_and_self_closing_svg_stay_valid(self):
        text='<HTML LANG="en"><body><p>A &amp; B</p></body></HTML>'
        translated=locale.convert_text(text,".html",lambda s:'<script>alert</script>' if s=="A & B" else None,"ko")
        self.assertIn('&lt;script&gt;',translated)
        self.assertNotIn('<script>',translated)
        self.assertNotIn('LANG="en"',translated)
        svg=locale.convert_text('<svg xmlns="http://www.w3.org/2000/svg"/>',".svg",lambda s:None,"ja")
        self.assertEqual(kit.scan_svg(svg)[0][1]["lang"],"ja")


    def test_glossary_prefills_terms_and_rejects_avoided_phrases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"src"; root.mkdir()
            (root/"report.md").write_text("# Key issues\n\nThe app uses the MySQL dialect.\n")
            decision={"output_language":"ko","language_source":"request"}
            plan=locale.prepare(root,decision)
            self.assertEqual(plan["glossary"]["path"],"glossary.ko.json")
            key_unit=next(u for u in plan["units"].values() if u["source"]=="Key issues")
            self.assertEqual(key_unit["target"],"핵심 문제")
            self.assertTrue(key_unit.get("from_glossary"))
            sentence=next(u for u in plan["units"].values() if u["source"].startswith("The app uses"))
            sentence["target"]="앱이 MySQL 방언을 사용합니다."
            with self.assertRaises(ValueError):
                locale.apply(plan,Path(temporary)/"out-bad")
            sentence["target"]="앱이 MySQL 호환 SQL을 사용합니다."
            locale.apply(plan,Path(temporary)/"out")
            self.assertIn("핵심 문제",(Path(temporary)/"out"/"report.md").read_text())

    def test_preserved_target_language_text_follows_the_glossary_and_list_numbers_are_not_prose(self):
        """Text the agents wrote in Korean (key issues, Specialist SA Agent advice) is preserved, so the glossary's replaced
        terms are checked on it too; a renderer list line "**1. Title** (a · b)" keeps its separators; a rendering that keeps
        the English name ("Pod Identity(...)") is accepted."""
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"src"; root.mkdir()
            (root/"report.md").write_text("# Key issues\n\n경보 대신 알람이라고 쓴 문장입니다.\n\nPod Identity\n")
            plan=locale.prepare(root,{"output_language":"ko","language_source":"request"})
            korean=next(u for u in plan["units"].values() if u["source"].startswith("경보 대신"))
            korean["preserve"]=True
            pod=next(u for u in plan["units"].values() if u["source"]=="Pod Identity")
            self.assertEqual(pod["target"],"Pod Identity(파드에 IAM 역할을 주는 EKS 기능)")
            with self.assertRaises(ValueError):
                locale.apply(plan,Path(temporary)/"out-bad")
            korean["preserve"]=False; korean["target"]="경보라고 고쳐 쓴 문장입니다."
            locale.apply(plan,Path(temporary)/"out")
            self.assertIn("Pod Identity(파드에",(Path(temporary)/"out"/"report.md").read_text())
        self.assertEqual(locale.glossary_violations("**1. 경보가 사람에게 닿게 하고 기본 경보를 갖추기** (며칠 작업 · 비용 증가)",{" · ":"x"},False),[])
        terms={"Suggestion":"제언","Alternatives":"대안"}
        self.assertEqual(locale.untranslated_label("**Suggestion** 먼저 구독을 추가합니다.",terms,"ko"),"Suggestion")
        self.assertEqual(locale.untranslated_label("Alternatives: 예약 조정을 쓰는 방안.",terms,"ko"),"Alternatives")
        self.assertIsNone(locale.untranslated_label("Amazon ECS 서비스는 그대로 둡니다.",terms,"ko"))
        self.assertEqual(locale.glossary_violations("첫 문장입니다. 둘째 문장 · 나열",{" · ":"x"},False),[" · "])


if __name__=="__main__":
    unittest.main()
