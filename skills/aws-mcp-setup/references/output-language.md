# Output language and localization

Skill definitions, references, canonical templates and code stay in English.

## One language per run

A run has one report language: **English unless the person gave `lang <tag>`** (for example `lang ko`, or `4 ko` in
the scope interview). The language the person happens to write in does not change it, and neither do the AWS region,
the OS locale or the account. `scope.json` records it as `output_language`; every stage reads it from there.

Every artifact the agents write is **English**, whatever the report language: manual checks, key issues, Specialist SA
Agent advice and candidates, list-price labels, diagram graph labels and captions. The validators refuse text in another
language. For a report in English, `06-assessment/assessment.html` is the deliverable. For another language, the
finished English report is translated once, at the end, into `06-assessment-<tag>/assessment.html`; that folder is the
deliverable. Nothing is ever half translated: the English draft is all English and the localized copy is all in the
target language, and the layout check flags English sentences left in it.

Record the decision for the helper:

```bash
python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" language \
  --language "<scope.json output_language>" --output "$RUN_DIR/output-language.json"
```

## Translating the finished report (report language other than English)

The helper does not translate; the agent does, in parallel chunks, and the helper checks every unit.

1. Finish canonical reports and source HTML/SVG, preserving the original snapshot.
   Paths are fixed so every run looks the same: `CANONICAL_RUN="$RUN_DIR/06-assessment"`,
   `LOCALIZATION_WORK="$RUN_DIR/localization-work"`, `LOCALIZED_RUN="$RUN_DIR/06-assessment-<language>"`
   (for Korean `06-assessment-ko`). `apply` refuses any other output folder for `06-assessment`.
2. Prepare a translation plan outside the source bundle. `--reuse` carries the translations of an earlier plan of the
   same language (a re-render of the same run) so only changed text is translated again; glossary terms are pre-filled:

   ```bash
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" prepare \
     --source "$CANONICAL_RUN" --context "$RUN_DIR/output-language.json" \
     --output "$LOCALIZATION_WORK/ko.plan.json" [--reuse "$LOCALIZATION_WORK/ko.plan.previous.json"]
   ```

3. Split what is left into chunks with a brief and the glossary, translate the chunks in parallel, merge, and list every
   unit the checker would refuse:

   ```bash
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" chunks --plan "$LOCALIZATION_WORK/ko.plan.json" --folder "$LOCALIZATION_WORK/chunks"
   # one sub-agent per chunk-NN.src.json (Kiro subagent, Claude Code Task, Codex agent), all at once, with this prompt:
   #   Read $LOCALIZATION_WORK/chunks/BRIEF.md and follow it. Translate chunk-NN.src.json into chunk-NN.ko.json.
   #   Run the validation line from the brief and reply with its output only.
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" merge --plan "$LOCALIZATION_WORK/ko.plan.json" --folder "$LOCALIZATION_WORK/chunks"
   python3 "$TOOLKIT_SKILL/scripts/localize_artifacts.py" check --plan "$LOCALIZATION_WORK/ko.plan.json"
   ```

   `chunks` keeps units without words (numbers, IDs) as they are and sends the rest. `merge` marks a target equal to its
   source as preserved. `check` exits 3 with every remaining problem and its reason (a changed number or ID, a glossary
   phrase to avoid, an English label left in place); fix those targets in the plan and run `check` again until it
   passes. Without a sub-agent tool, translate the chunks yourself one after another.
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
