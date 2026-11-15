# Validation history

Initial validation: **2026-10-16, macOS arm64**. Later extensions are recorded separately; historical six-pathway results remain historical records.

## Initial package and installation

Five skill metadata checks and **20 functional tests** passed. Tests covered client paths, repeatable installation, dry-run, managed updates, conflict/local-edit preservation, MCP merging, credential isolation, account mismatch, pagination, query budgets, error/empty-result separation, raw checksums/references, UNKNOWN scoring, ranking gates and offline reports.

The macOS wrapper ran. The Linux wrapper received Bash syntax/dry-run checks on macOS, not native Linux execution. The Windows wrapper received code review, not native Windows execution. Installed copies and pinned companion assets were verified.

## Initial client and MCP verification

Claude Code 2.1.296.1183 recognized project configuration but awaited first-use approval. Codex CLI 0.155.1 recognized HTTP configuration; Kiro CLI 2.31.1 recognized registration. The endpoint completed initialize/initialized/tools-list using protocol 2025-03-26 and returned five tools. The macOS Python environment used a trusted system CA bundle with TLS verification retained.

Complete separate LLM sessions across all five stages were not exercised for every client. Configuration discovery, handshake and client-session validation are distinct claims.

## Initial lab assessment

AWS CLI 2.34.45 and Python 3.12.4 used temporary environment credentials with STS account verification. In us-east-1 across 11 requested services, collection produced **61 inventory items** from **37 logical CLI invocations: 36 successful, one not_found, no general errors/denials/skips**. HTTP retries/pagination were not counted separately.

The missing account-level S3 BPA configuration was preserved as NoSuchPublicAccessBlockConfiguration, so collection was partial; effective bucket/account controls were assessed together. Credentials were not written to source/config/reports. No resource changes, SSH/SQL execution or upstream marker-stack creation occurred.

The 35 checks were PASS 24, FAIL 4, NOT_APPLICABLE 2 and UNKNOWN 5. Scores describe the starter subset, not official WA coverage. All six then-current pathways were reported, with no confirmed ranking because business/source/cost evidence was missing. Container was a conditional pilot; other decisions remained unassessed.

D2 v0.9.0 rendered both graphs, then AWS-branded HTML/SVG/PNG redraws passed ID, self-check, geometry and visual inspection. Diagrams used 1280x720 SVG and 2560x1440 PNG. The focused current-state view grouped 20 of 61 items into five nodes and four relationships, retaining exclusions in fidelity metadata. Both reports displayed their diagrams without horizontal overflow. SVG screenshots used a local HTML image wrapper to avoid standalone-document timeouts.

Historical local artifacts remain under artifacts/lab-validation/final, excluded from distribution/Git.

## 2026-10-20: Modern DevOps

Added modern-devops as the seventh pathway. **24 tests passed**, covering preservation/upgrading of exact legacy six-pathway inputs, rejection of other missing/duplicate/unknown IDs, evidence/blocker/ranking rules and CLI upgrade notices. Bundle validation passed with zero errors. Three changed skills passed metadata checks using existing Python 3.13/PyYAML; default Python lacked PyYAML. Client copies were updated with backups.

The six WA pillars and six weighted criteria were retained. Historical reports were not rewritten; this feature work did not execute AWS queries/pipelines/deployments.

## 2026-10-20: GenAI/AI fit

Added 12 internal AI service-fit options while retaining seven pathways. **30 tests passed**, including unselected/unassessed defaults, catalog/reference validation, all-selected readiness, accelerator compatibility/benchmark requirements, legacy preservation/ranking deferral, report detail and HTML escaping. Output identifies ai_option_catalog_version 1.0.

Reviewed public product/developer sources and corrected the DevOps Agent source when a guessed document path redirected to its index. Synthetic helper/report validation did not execute agents, security tests, training, inference or accelerator benchmarks.

## 2026-10-20: Observability and messaging

Added three Modern DevOps observability options and four Cloud Native messaging options. **36 tests passed**, including compatibility, group isolation, selected readiness, reports and optional collection/relationships/limits/permission gaps. Optional services are amp,grafana,kafka,sqs,sns,events; defaults did not expand.

Fake CLI tests checked hashes/references, SQS page size and EventBridge target projection. Message receive/send/delete/purge, SNS publish and EventBridge put-events/replay are not allowlisted. Public AWS/OTel docs informed guidance, including EventBridge bus-model distinctions. No live account test of these new collectors occurred in that feature change.

## 2026-10-20: Host inspection

Added inspect_host.py, a fixed Linux probe and merge_host_evidence.py. **50 tests passed**, covering no-execution plans, target/account binding, strict SSH configuration, single-node SSM, status retries without resubmission, timeout behavior, filtered metadata, no source execution, symlink/size exclusions, no raw error-output persistence, hashes/references, preserved originals and duplicate prevention.

A macOS temporary-path symlink difference was corrected in the fixture to model the resolved /proc/cwd path. Bundle and three changed skill metadata checks passed. No actual SSH/SSM connection or live Linux probe was tested. Runtime Python, permissions, visibility and connectivity require verification in the target environment. SSM side effects and deployed-versus-loaded-code identity limitations are explicit.

## 2026-10-20: English skill content and report defaults

Converted all five project-managed skills, references, templates, UI metadata, service-catalog questions, generated report text, README and validation/source documentation to English. Preserved IDs, schemas, scores, evidence gates and Unicode-path test coverage. Historical account reports and pinned upstream companion content were not rewritten.

**52 tests passed**, including checks for English skill resources/default HTML language and normalized empty SQS results. All five skill metadata checks and bundle validation passed. During the requested us-east-1 assessment, an empty successful SQS response was confirmed independently and the list query normalized to an explicit QueueUrls array. A fresh snapshot retained the original attempt and completed without general query errors. Assessment artifacts remain excluded from Git.

## 2026-10-20: Request-language output localization

Superseded the final-English-output default while retaining English skill definitions and canonical helper drafts. All five skills now use explicit output preference first, then the language detected by the executing agent, with a shared language context.

Added localize_artifacts.py to prepare complete agent-authored translation plans and create fresh localized bundles. It localizes Markdown, HTML/SVG/accessibility text, embedded SVG and whitelisted JSON narratives; raw evidence, keys, enum values, scores, identifiers, code blocks and link destinations remain canonical. Missing translations or changed protected values fail before output creation. Stale raster previews and source-language QA require regeneration/review.

**59 tests passed.** The localization tests cover precedence, source hashes, coverage, protected numeric/ID/URL values, markup escaping, embedded diagrams, evidence preservation, English-copy fallback and separation from skill definitions. Korean and Japanese synthetic bundles were rendered and visually inspected with matching HTML language, localized body text and no horizontal overflow. The helper validates structure and declarations; the executing agent performs language detection and translation, and linguistic accuracy still requires review.

## 2026-10-20: Workload enrichment and assessment comparability

Revised the current-state, architecture-review and modernization skills following
comparison of two assessments of the same inventory. Full assessments now trace
workload-linked deployment/source evidence, record discovery outcomes, distinguish
declared and runtime facts inline, and produce workload-specific diagrams. Source
definitions remain English; generated narratives follow the request language.

Added an offline supplementary-evidence importer with account/region binding,
checksums, provenance, source revision, exit-status and payload validation. It
preserves original snapshots and collector invocation counts and exposes denied or
incomplete supplements. Review now rejects blank successful evidence and duplicate
rule/resource checks, preserves automatic baseline checks/scores, and records
scope/weight/status changes. Reports group findings by pillar and summarize
PASS/NOT_APPLICABLE results.

**68 tests passed**, including nine new synthetic regression tests for imports,
input preservation, invalid payloads, scope/status/provenance mismatches, source
versus runtime identity, scoring changes and baseline output. No live AWS queries,
source downloads or SSH/SSM operations were performed for this change. These tests
verify helper behavior; discovery quality and interpretation still require agent
judgment and review.

All five skill metadata checks and bundle validation passed. A synthetic review
was generated, rendered locally in Chrome and visually inspected for the new
basis disclosure, pillar grouping and inline confidence labels. Its missing
architecture diagram correctly remained labeled as pending. This was a helper
render check, not a new end-to-end assessment of the account.

## 2026-10-20: Live reassessment with enriched evidence

A new scoped read-only run collected 43 baseline CLI invocations and 43
supplementary invocations, including 12 source files pinned to repository commits.
The 86 evidence records retained one configuration-not-found response, one denied
query and one invalid/blank-output result. No host commands or resource changes
were performed. Korean reports and three D2-first AWS diagrams passed evidence,
score, graph, browser and visual checks; 31 local links resolved.

The live run exposed a CLI-zero/invalid-payload distinction. The importer now
preserves the real zero exit code while accepting an explicit validation_error
with status=error. The current offline suite passed **74 tests**. CloudFormation
WaitConditionHandle signed URL queries were removed from retained metadata, with
redacted field paths and pre-redaction hashes recorded. The enrichment reference
now calls out these capability-bearing resource identifiers. Account artifacts
remain local and excluded from Git.

## Visual HTML reports (2026-10-20)

Added `scripts/report_kit.py` (byte-identical in aws-architecture-review and
aws-modernization-pathway) and rebuilt the HTML renderers in `review.py` and
`recommend.py`: hero and KPI tiles, pillar-score, judgement, severity and inventory
charts, criteria heat map, option grids and an ordinal roadmap. Charts are inline SVG
with `role="img"`, title and description; no JavaScript, external fonts or assets are
used, and every value is read from the JSON artifacts. Markdown drafts are unchanged.

**74 tests passed**, including six new tests for kit parity, chart accessibility and
escaping, report sections, draft/diagram states and compatibility with the
localization prose parser. The redesigned reports were regenerated from an existing
snapshot (56 checks, seven pathways, final diagrams) and inspected in Chromium; the
earlier Korean deliverables of that run were agent-authored and remain unchanged.

## Run provenance (2026-10-20)

Added `aws-mcp-setup/scripts/run_metadata.py` (init/add/import-kiro-stats/show) and a
`--run-metadata` option on `review.py` and `recommend.py`. The reports now show the LLM model,
total inference time and total tokens declared by the agent (for example from a Kiro CLI
`/stats save` export) and store them in findings.json / pathways.json. Values are validated
(non-negative, integer tokens, schema 1.0), totals are summed across segments, partial
coverage is flagged, and absent values render as "not recorded"; nothing is estimated.

Importers exist for all three supported clients: Claude Code session transcripts (`message.usage`
per assistant record, deduplicated by uuid), Codex rollouts (cumulative `token_count` deltas) and
Kiro `/stats` exports; each reads only timestamps, model names and token counts. Fixtures are
synthetic and follow the publicly documented formats; real client logs were not shipped.

**79 tests passed**, including five new tests for the recorder, loader validation, report
integration (with and without metadata) and the Claude Code and Codex importers.

## Guide-driven installation (2026-10-20)

Added the `aws-skills-install` skill: a SKILL.md the client follows to copy the five skills,
write `.moda-install.json` stamps, verify SHA-256 hashes, merge the per-client MCP snippet and
fetch pinned companions, plus `assets/install-manifest.json` (every file with size and hash,
per-skill digest identical to `setup_toolkit.digest_tree`) and `scripts/build_manifest.py`
(`--check` fails when the manifest or the bootstrap copies are stale). Bootstrap copies of the
installer skill are tracked under `.claude/skills`, `.agents/skills` and `.kiro/skills` so a
fresh clone exposes it to each client before anything else is installed; other client files
stay ignored. `validate_bundle.py` includes the installer skill.

**82 tests passed**, including three new tests: manifest currency and bootstrap parity, per-file
hash coverage, and a simulated manual install that the scripted installer recognises as
unchanged while a hand-edited file is still preserved.

## Installer scripts removed (2026-10-20)

`scripts/install.py` and the `installers/` wrappers were removed; the guide-driven
`aws-skills-install` skill is the only installation path. `setup_toolkit.py` keeps the
companion fetch and MCP merge, `doctor.py` keeps verification, and the stamp digest is
unchanged, so earlier scripted installations remain recognised as managed. The
install-CLI test was replaced by a `setup_toolkit.py --configure-mcp` round-trip test.

**82 tests passed**; bundle validation and `build_manifest.py --check` passed.

## Unified assessment report, grades and Service Screener import (2026-10-26)

Reviewer feedback on the ecommerce-demo deliverables (unnatural wording, scores without
meaning, no at-a-glance view, missing not-assessed rationale, request for a security chapter
with Service Screener) led to:

- `assessment_report.py` (aws-modernization-pathway): one report ordered for the reader - key
  issues (agent-authored, must cite finding IDs), current architecture, qualitative pillar grades
  with the driving rules, findings, a security chapter with local and imported Service Screener
  findings side by side, modernization directions with explicit "not recommended / not assessed"
  reasons, reference material, a folded evidence table, and collapsed method, numeric scores and
  run provenance. Validated IaC changes go to iac-changes.json; the order of work is left to the
  meeting with the owner, so the unified report carries no roadmap.
- `report_kit.grade_pillar`: deterministic rubric (Fix first / Needs work / Watch / Sound / Unassessed; keys critical, weak, watch, sound, unassessed)
  documented in review-method.md; numeric scores remain in the appendix for pathway ranking.
- `import_service_screener.py` (aws-architecture-review): read-only import of `api-full.json`
  (schema verified at upstream commit 055c882), account/region guards, byte-identical copy with
  hash, `source: service-screener` findings with upstream severity kept separate.
- `glossary.ko.json` and Korean style rules; `localize_artifacts.py` pre-fills glossary terms and
  rejects avoid-phrases in prose. Status colours were adjusted for contrast.

The ecommerce-demo report was regenerated in English and localized to Korean through the plan
workflow, then inspected in Chromium. Service Screener import was exercised with a synthetic
`api-full.json`; no real Screener run exists for that account.

**87 tests passed** (five new: grade rubric, importer mapping and guards, unified report sections
and invented-reference refusal, side-by-side security chapter, glossary pre-fill and rejection).


## Live runs in three clients and read-only guard checks (2026-11-15)

One full assessment in each client, started at the same time against the same account
(7 CloudFormation stacks, 71 resources in scope, us-east-1, no host inspection). The profile was an
IAM user with long-term keys and write access, so each run stopped at the write-capability check
and continued only after the person's decision was recorded.

| Client | Model | Stage 1 to the last stage file | Report |
|---|---|---|---|
| Kiro CLI 3.3.0 | claude-opus-5.5, effort high | 145 min, 632.64 credits | Final |
| Claude Code 2.1.309 (Amazon Bedrock) | Opus 5.5, effort xhigh | 66 min, US$38.38 by `/cost` | Final |
| Codex CLI 0.155.1 (Amazon Bedrock) | openai.gpt-6-astra, effort high | 46 min | Final |

After each run the agent tried nine actions the guard must stop: `create-tags --dry-run` direct,
inside `bash -c` and through inline boto3; `get-secret-value`; a profile switch through
`AWS_PROFILE`; a read not in the approved list (then approved with `plan_check.py` and run); a
shell write under `~/.aws`; and file-tool edits of an installed `SKILL.md` and of `scope.json`. All
nine behaved as intended in every client, including the Codex `Bash` and `apply_patch` matchers.
CloudTrail showed no `CreateTags` or `GetSecretValue` call afterwards. Kiro's session had no
`use_aws` tool, so that hook was not exercised.

Fixes from these runs: scope discovery runs its calls in parallel (about 20 seconds for one region,
45 seconds for 17 regions, where Kiro's shell had stopped it at 120 seconds) and the skill passes
the named regions; the report's run length ends at the last file in the stage folders instead of
the first render (Codex had shown 18 minutes for a 46-minute run); the write audit names the tool
that made each change, because all three audits listed changes from a deployment running in
another session under the same IAM user; the person's reason for continuing with a write-capable
profile is taken in their own words; context answers are not inferred from other replies; and the
renderer is installed in the project folder rather than borrowed from another one. Step-by-step
guides for the three clients are in [docs/guides](guides/).

**200 tests passed.**
