# Output language and localization

Skill definitions, references, canonical templates and code stay in English.
Human-facing results follow the user's language. A request to write **skills**
in English is not a request for English **reports**.

## Detect once and reuse

The executing agent detects the natural language of the user's actual request,
ignoring quoted source text, code, service names and English skill instructions.
Honor an explicit output-language preference first; otherwise use the request's
main language. Ask only when genuinely ambiguous. Do not infer language from
AWS region, OS locale, account identity or code.
If the request is only a skill name, code or a copied invocation template, reuse
the latest clear user-language preference or conversation language. An English
default_prompt is not an explicit request for English results.

Reuse the resolved language for setup summaries, current-state reports, findings
explanations, recommendations, roadmaps, diagram labels and final summaries.
Apply a later language change to presentation artifacts without changing evidence.
For multiple requested languages, derive separate bundles from the same canonical source.

The helper does not guess natural language or call a translation service. The
agent supplies its detected BCP-47-style tag, such as ko, en, ja, zh-hant, de or fr.
CLI precedence: explicit --language, agent-provided --request-language, inherited
context, then English only as a standalone-CLI fallback.

```bash
python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" language \
  --request-language ko --output "$RUN_DIR/output-language.json"
```

Use --language for an explicit override and --context to reuse a decision.
Store only the language tag/source, not the full request, which might contain credentials.

## Canonical drafts and final delivery

Deterministic helpers retain canonical schemas and may generate English intermediate
drafts. For a non-English request, those drafts are not the final deliverable.
Write final narrative in the resolved language or use the complete workflow below.

1. Finish canonical reports and source HTML/SVG, preserving the original snapshot.
   Paths are fixed so every run looks the same: `CANONICAL_RUN="$RUN_DIR/06-assessment"`,
   `LOCALIZATION_WORK="$RUN_DIR/localization-work"`, `LOCALIZED_RUN="$RUN_DIR/06-assessment-<language>"`
   (for Korean `06-assessment-ko`). `apply` refuses any other output folder for `06-assessment`.
2. Prepare a translation plan outside the source bundle:

   ```bash
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" prepare \
     --source "$CANONICAL_RUN" --context "$RUN_DIR/output-language.json" \
     --output "$LOCALIZATION_WORK/ko.plan.json"
   ```

3. The agent translates every unit's source into target. Set preserve=true only
   for intentionally unchanged product names, identifiers, timestamps or technical
   notation, or text already reviewed in the target language. Do not preserve English
   narrative to bypass translation. Targets are
   plain single-line text; the helper preserves structural markup. Missing units
   or changed protected identifiers, enum literals, numbers or URLs are errors.
4. Apply into a fresh directory outside the canonical source:

   ```bash
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" apply \
     --plan "$LOCALIZATION_WORK/ko.plan.json" --output "$LOCALIZED_RUN"
   ```

5. Review titles and body text for completeness, meaning and terminology. HTML lang
   alone is insufficient. Set appropriate CJK fallbacks or right-to-left layout
   when needed. Then check each localized page from the DOM, not from screenshots:

   ```bash
   node "$TOOLKIT_SKILL/scripts/render_artifact.mjs" "$LOCALIZED_RUN/assessment.html" "$LOCALIZATION_WORK/qa-assessment" --check
   node "$TOOLKIT_SKILL/scripts/render_artifact.mjs" "$LOCALIZED_RUN/details.html" "$LOCALIZATION_WORK/qa-details" --check
   ```

   `--check` renders the page at 1280 px and 390 px and records page overflow, elements
   wider than their container, clipped text, images and SVGs that did not render,
   same-page links without a target and English sentences left in a non-English page
   (`layout-check.json`). Exit code 0 is the pass; on failure it also writes `page.png`
   for the person, and you fix what the issue list names (an untranslated unit, a long
   unbroken label) and run it again. Diagrams drawn by `diagram.py` are SVG and are
   translated with the page; no raster to regenerate. Do not rerun an old English
   report-authoring script over localized output.
6. After language review and the layout check pass, set localization.json status=complete
   and record language_review/visual_qa (the layout-check.json status). Deliver the localized bundle. Pending
   translations, raster regeneration or QA mean the result is still incomplete.

The agent can supply any language it can translate faithfully; no fixed locale
catalog is imposed. Uncertain translations require review, not silent English
fallback. Structural validation does not prove linguistic accuracy or meaning.

## Glossary and style rules

`assets/glossary.<lang>.json` (currently `ko`) carries preferred renderings for recurring report
labels, phrases to avoid with the reason, and style rules. `prepare` attaches the glossary to the
plan and pre-fills units whose source is an exact glossary term; `apply` rejects a target that
contains an avoid-phrase (bullet separators are tolerated in short labels such as chips and
legends). Add run-specific terms under `glossary_extra` in the plan rather than editing the bundled
file during an assessment; propose durable additions through the repository.

Korean style rules the agent applies when translating narrative:

- One claim per sentence; do not string nouns together with `·` or `+` in body text.
- Explain, do not transliterate, English idioms (dialect, immutable artifact, local score).
- Introduce a technical term once with a short parenthetical, then reuse the Korean term.
- Keep the reader's order: what is wrong, why it matters, what to do, then the evidence.
- Keep service names, identifiers, numbers and URLs unchanged.

Review the localized report for these rules before marking `language_review` as passed; the
glossary check is mechanical and does not judge fluency.

## Preservation rules

Localize Markdown prose, HTML/SVG visible text and accessibility labels, including
embedded SVG. JSON localization is restricted to narrative fields in findings.json,
pathways.json, candidates.draft.json, checks.enriched.json, screener-findings.json,
key-issues.json and assessment-summary.json.
Keep IDs, scores, enum values, keys, counts, URLs and references unchanged.
Product names and literal codes can remain with a localized explanation.

Raw evidence, inventory/collection manifests, code blocks, commands, link destinations,
styles, SVG coordinates/data IDs and initial D2 render evidence stay canonical.
Final human-facing SVG/PNG diagrams must use the requested language; initial
graph/D2 sources remain references. Source hashes are checked and originals are
never overwritten.

Generated diagram PNGs and report previews are removed from the localized copy
for regeneration; ordinary bitmap assets remain. Source-language QA records are
archived and marked pending for the localized copy. A source QA pass is not a
localized-output QA pass.
If an ordinary bitmap contains language-specific prose, provide a localized asset
or regenerate it during review; changing its surrounding caption does not translate
text embedded in pixels.

Never localize SKILL.md or the project source tree for a result-language request,
translate raw evidence in place, or change recommendation states or numeric results.
Preserve historical reports.

Pass the resolved language to companion diagram/Bento workflows as well. This
helper targets static reports and the listed JSON narrative formats; it does not
translate text stored in application scripts, serialized deck state, PDF or DOCX.
Use those artifacts' native authoring workflows in the requested language and
verify their rendered output separately.
