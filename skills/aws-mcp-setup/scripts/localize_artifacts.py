#!/usr/bin/env python3
"""Resolve output language and apply agent-authored translations to artifact copies.

The agent detects the user's natural language and translates prose. This helper
validates coverage and protected values; it does not infer language or call an LLM.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import sys

NARRATIVE_KEYS = {
    "observation", "recommendation", "rationale", "problem", "use_case",
    "prerequisites", "blockers", "validation", "alternatives", "risks",
    "cost_assumptions", "owner", "input_notes", "assessment_reason",
    "title", "impact", "action",  # key-issues.json
}
PROTECTED = re.compile(
    r"https?://[^\s<>\"`]+|arn:[^\s<>\"`]+|\b\d{12}:[A-Za-z0-9:/_.-]+\b|\b\d{12}\b|"
    r"\b[a-z]{2}(?:-[a-z]+)+-\d+\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|"
    r"\b\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z-]+)?\b|"
    r"\b(?:i|sg|vol|vpc|subnet|igw|nat|rtb|vpce)-[0-9a-f]+\b|"
    r"\b[efhs]\d{4,}\b|\b(?:PASS|FAIL|WARN|UNKNOWN|NOT_APPLICABLE)\b|"
    r"\b(?:candidate|conditional|not_recommended|not_assessed|confirmed|blocked)\b|"
    r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)*%?(?![A-Za-z0-9_])",
    re.ASCII,
)
SKIP_TAGS = {"script", "style", "code", "pre"}
# Status values that are also ordinary English words: protected only when the unit is the value itself (a chip or a
# table cell), not inside a sentence ("a confirmed subscriber", "the conditional branch").
PLAIN_WORD_ENUMS = {"candidate", "conditional", "confirmed", "blocked"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def protected_tokens(text, whole_unit):
    found = PROTECTED.findall(text)
    if not whole_unit:
        found = [t for t in found if t not in PLAIN_WORD_ENUMS]
    return Counter(found)


def protected_match(source, target):
    """Identifiers, URLs, numbers and status values must survive translation. A month written as a word in the source
    ("Mar 2026") may become a number in the target (year 2026, month 3)."""
    whole = source.strip() in PLAIN_WORD_ENUMS
    want, got = protected_tokens(source, whole), protected_tokens(target, whole)
    if want == got:
        return True
    months = Counter(str(MONTHS[m.lower()[:3]]) for m in re.findall(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b", source))
    return bool(months) and want + months == got


def digest(data):
    return hashlib.sha256(data).hexdigest()


def language_tag(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value):
        raise ValueError("language must be a BCP-47-style tag such as en, ko or ja")
    return value.lower()


def resolve_language(explicit="auto", request=None, inherited=None):
    if explicit != "auto":
        return {"output_language": language_tag(explicit), "language_source": "explicit"}
    if request:
        return {"output_language": language_tag(request), "language_source": "request"}
    if inherited:
        return {"output_language": language_tag(inherited["output_language"]), "language_source": "inherited"}
    return {"output_language": "en", "language_source": "standalone_cli_fallback"}


def translatable(text):
    stripped = text.strip()
    if not stripped or not re.search(r"[^\W\d_]", stripped, re.UNICODE):
        return False
    # Entire identifiers, URLs, literal enum values and file references stay intact.
    if PROTECTED.fullmatch(stripped):
        return False
    if re.fullmatch(r"[\w./:@+-]+\.(?:json|md|html|svg|png|py|mjs|d2)", stripped):
        return False
    return True


class ProseParser(HTMLParser):
    """Locate raw prose spans while leaving tags, styles and identifiers intact."""
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.starts = [0]
        self.starts.extend(m.end() for m in re.finditer("\n", text))
        self.stack = []
        self.spans = []
        self.embedded = []
        self.feed(text)

    def current_offset(self):
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        offset = self.current_offset()
        for match in re.finditer(r"""([:\w-]+)\s*=\s*(["'])(.*?)\2""", raw, re.S):
            key, value = match.group(1).lower(), match.group(3)
            a, b = offset + match.start(3), offset + match.end(3)
            if key in {"alt", "title", "aria-label"}:
                self.spans.append((a, b, "attribute"))
            if key in {"src", "href"} and value.startswith("data:image/svg+xml;base64,"):
                self.embedded.append((a, b, value))
        if tag not in {"img", "meta", "link", "br", "hr", "input", "col", "source", "wbr"}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def prose(self, raw):
        if any(tag in SKIP_TAGS for tag in self.stack):
            return
        a = self.current_offset()
        if self.spans and self.spans[-1][1] == a and self.spans[-1][2] == "text":
            self.spans[-1] = (self.spans[-1][0], a + len(raw), "text")
        else:
            self.spans.append((a, a + len(raw), "text"))

    def handle_data(self, data):
        self.prose(data)

    def handle_entityref(self, name):
        self.prose("&" + name + ";")

    def handle_charref(self, name):
        self.prose("&#" + name + ";")


def markdown_spans(text):
    spans = []
    offset = 0
    fence = None
    token = re.compile(r"(`+).*?\1|!?\[([^\]]*)\]\(([^)]*)\)|https?://[^\s<>]+|<[^>]+>", re.S)
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            offset += len(line)
            continue
        if fence or re.fullmatch(r"[\s|:*-]+", line):
            offset += len(line)
            continue
        # Split table cells and preserve Markdown structural prefixes.
        cursor = 0
        def prose(a, b):
            for part in re.finditer(r"[^|]+", line[a:b]):
                start, end = a + part.start(), a + part.end()
                prefix = re.match(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)?", line[start:end])
                spans.append((offset + start + prefix.end(), offset + end, "markdown"))
        for match in token.finditer(line):
            prose(cursor, match.start())
            if match.group(2) is not None:  # Translate a link label, never its destination.
                label = match.group(2)
                if "`" not in label:
                    spans.append((offset + match.start(2), offset + match.end(2), "markdown"))
            cursor = match.end()
        prose(cursor, len(line))
        offset += len(line)
    return spans


def json_slots(data, path=(), narrative=False):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from json_slots(value, path + (key,), key in NARRATIVE_KEYS)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from json_slots(value, path + (index,), narrative)
    elif narrative and isinstance(data, str):
        yield path, data


def unit_id(source):
    return digest(source.encode("utf-8"))


def convert_text(text, suffix, visit, language=None, depth=0):
    if suffix in {".html", ".svg"}:
        parser = ProseParser(text)
        spans = parser.spans
    else:
        parser = None
        spans = markdown_spans(text)
    edits = []
    for a, b, mode in spans:
        raw = text[a:b]
        source = html.unescape(raw) if mode != "markdown" else raw
        stripped = source.strip()
        if not translatable(stripped):
            continue
        target = visit(stripped)
        if target is None or target == stripped:
            continue
        if mode == "markdown":
            target = html.escape(target, quote=False)
            target = re.sub(r"([\\`|[\]])", r"\\\1", target)
        else:
            target = html.escape(target, quote=mode == "attribute")
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        edits.append((a + leading, b - trailing if trailing else b, target))
    if parser:
        import base64
        for a, b, value in parser.embedded:
            if depth >= 2:
                raise ValueError("embedded SVG nesting exceeds the supported limit")
            source = base64.b64decode(value.split(",", 1)[1], validate=True).decode("utf-8")
            # Do not localize decorative icons without visible text.
            if "<text" not in source:
                continue
            target = convert_text(source, ".svg", visit, language, depth + 1)
            if language:
                edits.append((a, b, "data:image/svg+xml;base64," + base64.b64encode(target.encode()).decode()))
    for a, b, target in sorted(edits, reverse=True):
        text = text[:a] + target + text[b:]
    if language and parser:
        root_tag = "html" if suffix == ".html" else "svg"
        match = re.search(r"<" + root_tag + r"\b[^>]*>", text, re.I)
        if match:
            original = match[0]
            replacement = re.sub(r"""\s(?:xml:)?lang\s*=\s*(["']).*?\1""", "", original, flags=re.I)
            closing = "/>" if replacement.endswith("/>") else ">"
            replacement = replacement[:-len(closing)] + f' lang="{language}"' + closing
            text = text[:match.start()] + replacement + text[match.end():]
    return text


def artifact_kind(path):
    if "raw" in path.parts:
        return None
    if path.name.endswith(".d2.svg") or any(part.startswith(".") or part == "qa-d2" for part in path.parts):
        return None  # Initial-render evidence and temporary previews stay canonical.
    if path.suffix in {".md", ".html", ".svg"}:
        return path.suffix
    if path.name in {"findings.json", "pathways.json", "candidates.draft.json", "checks.enriched.json",
                     "checks.baseline.json", "assessment-basis.json", "screener-findings.json", "key-issues.json",
                     "assessment-summary.json"}:
        return ".json"
    return None


def source_files(root):
    if not root.is_dir():
        raise ValueError("source artifact directory does not exist")
    if (root / "SKILL.md").exists() or (root / "skills").is_dir():
        raise ValueError("localize assessment artifacts, not skill definitions or the project root")
    if (root / "localization.json").exists():
        raise ValueError("create each locale from the canonical bundle, not another localized copy")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("artifact bundle symlinks must be resolved explicitly")
        if path.is_file():
            files.append(path)
    return files


GLOSSARY_DIR = Path(__file__).resolve().parents[1] / "assets"


def load_glossary(language):
    """Return the bundled glossary for the language's primary tag, or None when none exists."""
    path = GLOSSARY_DIR / f"glossary.{language.split('-')[0]}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"path": path.name, "terms": data.get("terms", {}), "avoid": data.get("avoid", {}), "style": data.get("style", [])}


def glossary_violations(target, avoid, allow_bullets):
    """Avoid-phrases found in a translated target.

    Separator rules (" · ", "+") apply only to prose, detected by sentence punctuation; labels, chips,
    legends and metadata lines such as "Findings f0001 · Evidence e0001" keep their separators.
    """
    # A leading list number ("**1. Title** (Days of work · Cost up)") is not a sentence end.
    prose = bool(re.search(r"[.!?\u3002]\s|[.!?\u3002]$", re.sub(r"^[\W_]*\d+\.\s+", "", target.strip())))
    hits = []
    for phrase in avoid:
        if phrase in (" · ", "+") and (allow_bullets or not prose):
            continue
        if phrase == "+":
            # a sign or a time-zone offset ("+00:00", "+3"), not a separator between nouns
            if re.search(r"\+(?!\d)", target):
                hits.append(phrase)
            continue
        if phrase in target:
            hits.append(phrase)
    return hits


def untranslated_label(source, terms, language):
    """A glossary label left in English: the whole unit is a term, or it contains a multi-word term."""
    if language.split("-")[0] == "en" or not terms:
        return None
    text = PROTECTED.sub(" ", source).strip(" :·-*|()[]")
    if text in terms and terms[text] != text:
        return text
    # A report label in front of text written in the target language ("**Suggestion** ...", "Alternatives: ...").
    lead = re.match(r"^[\s*_#>-]*([A-Z][A-Za-z &/-]{1,40}?)(?:\*\*|:)(?:\s|$)", source)
    if lead and lead.group(1) in terms and terms[lead.group(1)] != lead.group(1):
        return lead.group(1)
    for term, target in terms.items():
        if " " in term and term != target and term not in target and re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", source):
            return term
    return None


def prepare(root, decision):
    root = root.resolve()
    units, hashes = {}, {}
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        hashes[relative] = digest(path.read_bytes())
        kind = artifact_kind(path.relative_to(root))
        if kind is None:
            continue
        def visit(source):
            if translatable(source):
                key = unit_id(source)
                unit = units.setdefault(key, {"source": source, "target": None,
                    "preserve": decision["output_language"].split("-")[0] == "en", "files": []})
                if relative not in unit["files"]:
                    unit["files"].append(relative)
            return None
        text = path.read_text(encoding="utf-8")
        if kind == ".json":
            for _, source in json_slots(json.loads(text)):
                visit(source)
        else:
            convert_text(text, kind, visit)
    glossary = load_glossary(decision["output_language"])
    for unit in units.values():  # pre-fill exact glossary matches so recurring labels stay consistent
        if glossary and unit["source"] in glossary["terms"] and not unit["preserve"]:
            unit["target"] = glossary["terms"][unit["source"]]
            unit["from_glossary"] = True
    return {"schema_version": "1.0", **decision, "source_language": "en", "source_root": str(root),
            "source_hashes": hashes, "glossary": glossary, "glossary_extra": {}, "units": units, "status": "translation_required"}


def expected_output(root, language):
    """The one place a localized assessment goes: next to 06-assessment as 06-assessment-<language>, so every run, client
    and status helper finds it in the same folder."""
    return root.parent / f"{root.name}-{language.split('-')[0].lower()}"


def apply(plan, output):
    language = language_tag(plan["output_language"])
    root = Path(plan["source_root"]).resolve()
    if output.exists() or output.resolve().is_relative_to(root):
        raise ValueError("use a fresh output directory outside the source bundle")
    if root.name == "06-assessment" and output.resolve() != expected_output(root, language).resolve():
        raise ValueError(f"localized assessment goes to {expected_output(root, language)} (06-assessment-<language> next to the source)")
    current = {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in source_files(root)}
    if current != plan["source_hashes"]:
        raise ValueError("source artifacts changed; prepare a fresh translation plan")
    def translate(source):
        unit = plan["units"].get(unit_id(source))
        if not unit or unit.get("source") != source:
            raise ValueError("translation unit is missing or mismatched")
        if unit.get("preserve") is True:
            if unit.get("target") not in (None, source):
                raise ValueError("preserved units cannot provide a different translation")
            label = untranslated_label(source, (plan.get("glossary") or {}).get("terms", {}), language)
            if label:
                raise ValueError(f"report label {label!r} must be translated, not preserved (unit: {source[:40]!r})")
            # Text already written in the target language (key issues, specialist advice) is preserved, so the glossary's
            # replaced terms are checked here too; separator rules apply to translations only.
            avoid = dict((plan.get("glossary") or {}).get("avoid", {}))
            hits = [h for h in glossary_violations(source, avoid, allow_bullets=True) if h not in (" · ", "+")] if language.split("-")[0] != "en" else []
            if hits:
                raise ValueError(f"preserved text uses a phrase the glossary asks to avoid: {', '.join(hits)}; give a target with the glossary term (unit: {source[:40]!r})")
            return source
        target = unit.get("target")
        avoid = dict((plan.get("glossary") or {}).get("avoid", {}))
        if isinstance(target, str) and avoid:
            hits = glossary_violations(target, avoid, allow_bullets=len(target) <= 40)
            if hits:
                raise ValueError(f"translation uses a phrase the glossary asks to avoid: {', '.join(hits)} (unit: {source[:40]!r})")
        if not isinstance(target, str) or not target.strip() or "\n" in target or "\r" in target:
            raise ValueError("every prose unit needs a translation or explicit preserve=true")
        if target == source and language != "en":
            raise ValueError("unchanged wording must be explicitly marked preserve=true")
        terms = (plan.get("glossary") or {}).get("terms", {})
        for term, rendering in terms.items():
            if (language.split("-")[0] != "en" and " " in term and term != rendering and term not in rendering  # "Pod Identity(...)" keeps the name
                    and re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", target)):
                raise ValueError(f"translation keeps the English label {term!r}; use {rendering!r} (unit: {source[:40]!r})")
        if not protected_match(source, target):
            raise ValueError("translation changed protected identifiers, enum values, URLs or numbers")
        return target
    # Validate every unit and render in memory before creating output.
    for unit in plan["units"].values():
        translate(unit["source"])
    rendered = {}
    for relative in current:
        path = root / relative
        kind = artifact_kind(Path(relative))
        if kind is None:
            continue
        text = path.read_text(encoding="utf-8")
        if kind == ".json":
            data = json.loads(text)
            for pointer, source in list(json_slots(data)):
                if not translatable(source):
                    continue
                parent = data
                for key in pointer[:-1]:
                    parent = parent[key]
                parent[pointer[-1]] = translate(source)
            rendered[relative] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        else:
            rendered[relative] = convert_text(text, kind, translate, language)
    shutil.copytree(root, output)
    for relative, text in rendered.items():
        (output / relative).write_text(text, encoding="utf-8")
    # Preserve ordinary bitmap assets; invalidate only generated diagram/previews.
    stale_rasters = []
    for path in output.rglob("*.png"):
        relative = path.relative_to(output)
        if "raw" in relative.parts or "qa-d2" in relative.parts:
            continue
        base = relative.with_suffix("")
        paired = any(str(base) + ext in rendered for ext in (".svg", ".html"))
        preview_base = re.sub(r"\.(?:preview|top)$", "", str(base))
        preview = preview_base != str(base) and preview_base + ".html" in rendered
        if path.name == "report-preview.png":
            preview = preview or any(
                str(relative.parent / name) in rendered
                for name in ("review.html", "recommendations.html", "assessment.html", "report.html")
            )
        if paired or preview:
            stale_rasters.append(path.relative_to(output).as_posix())
            path.unlink()
    # Earlier QA applies to the source language, not the new presentation.
    stale_qa = {"qa-summary.json", "browser-qa.json", "browser-check.json",
                "diagram-audit.json", "self-check-results.json"}
    for path in list(output.rglob("*.json")):
        relative = path.relative_to(output)
        if path.name not in stale_qa or "raw" in relative.parts or "qa-d2" in relative.parts:
            continue
        backup = output / ".localization-source-qa" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_text(json.dumps({"status": "requires_localization_qa",
            "source_qa": backup.relative_to(output).as_posix()}) + "\n")
    record = {"schema_version": "1.0", "output_language": language,
              "language_source": plan["language_source"], "source_language": plan["source_language"],
              "created_at": datetime.now(timezone.utc).isoformat(), "translated_files": sorted(rendered),
              "units": len(plan["units"]), "rasters_to_regenerate": stale_rasters,
              "status": "requires_visual_qa", "original_evidence_preserved": True}
    (output / "localization.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("language", "prepare", "apply"))
    p.add_argument("--language", default="auto")
    p.add_argument("--request-language", help="Language detected by the calling agent, not inferred from the OS or AWS region")
    p.add_argument("--context", type=Path)
    p.add_argument("--source", type=Path)
    p.add_argument("--plan", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        if args.action == "apply":
            if not args.plan:
                p.error("--plan is required")
            result = apply(json.loads(args.plan.read_text(encoding="utf-8")), args.output)
        else:
            inherited = json.loads(args.context.read_text(encoding="utf-8")) if args.context else None
            decision = resolve_language(args.language, args.request_language, inherited)
            if args.action == "prepare":
                if not args.source:
                    p.error("--source is required")
                if args.output.resolve().is_relative_to(args.source.resolve()):
                    raise ValueError("write the translation plan outside the source bundle")
                result = prepare(args.source, decision)
            else:
                result = decision
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        print(json.dumps({"output": str(args.output), "output_language": result["output_language"],
                          "status": result.get("status", "resolved")}))
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(f"Localization stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
