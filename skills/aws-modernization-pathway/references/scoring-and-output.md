# Candidate and output contract

candidates.draft.json contains seven pathway objects. Input snapshot schema_version remains 1.0; pathway_catalog_version 2.0 identifies the seven-pathway output catalog. The helper validates inputs and generates pathways.json plus Markdown/HTML reports. The HTML report is visual by default: KPI tiles, verified-weight bars, pathway cards, a criteria heat map (unknown scores hatched), option grids for the AI/observability/messaging catalogs, an ordinal roadmap derived from candidate waves, and collapsible pathway details. It is rendered by `scripts/report_kit.py` (standard library, inline SVG, no scripts) from pathways.json only; it never adds scores, dates or ranks that the data lacks. With `--run-metadata`, both reports and pathways.json carry the agent-declared LLM model, inference time and token totals (aws-mcp-setup [run metadata](../../aws-mcp-setup/references/run-metadata.md)).

Each candidate includes pathway, status, resource_ids, finding_ids, problem, rationale, target_services, alternatives, prerequisites, blockers, criteria, validation, rollback, wave, owner, cost_assumptions and source_urls.

Pathway IDs are container, serverless, managed-database, managed-analytics, genai, cloud-native and modern-devops. Status is candidate, conditional, not_recommended or not_assessed. References must resolve to the supplied inventory/findings.

## Six shared weighted criteria

| Criterion | Weight | Meaning of a score of 5 |
|---|---:|---|
| business_value | 25 | Direct contribution to verified business goals |
| technical_fit | 25 | Key compatibility/execution constraints verified |
| operating_model | 15 | Clear ownership and sufficient operating capability |
| risk_manageability | 15 | Migration/security/data risks and rollback verified |
| delivery_feasibility | 10 | Feasible team, schedule and dependency plan |
| cost_confidence | 10 | Evidence for current, transition and operating cost |

Each criterion has a score from 1 to 5 or null, plus rationale. Weighted fit is the sum of weight times score/5. Produce a numeric score only when all criteria are known; otherwise score is null and known_weight_percent records coverage. Never replace unknowns with zero.

A candidate is rank-eligible only when status is candidate, blockers are empty and all criteria are known, subject to selected-option checks below. Conditional diagnostic scores do not imply a confirmed rank. These are local judgments, not official AWS scores.

Modern DevOps uses the same criteria. Tool names or pipeline presence are not sufficient evidence. target_services may include existing CI/CD and IaC tools; do not force AWS product adoption. Waves should follow evidence/dependencies with explicit validation, rollback and ownership.

Score criteria independently. Source compatibility can support a conditional
technical hypothesis; it does not verify team capacity, delivery duration, rollback
or cost. Unknown business demand means not_assessed, not automatically a low score
or not_recommended. A documented lack of fit can support not_recommended.

Keep source/template qualifiers and deployed-version gaps in the option rationale.
Propose component-specific changes where supported, and include a retain/improve
alternative. Do not infer a need for Kubernetes, agents, streaming or a managed
database merely from product availability.

Rollback must address state as well as routing. For a database cutover, identify
the write authority before/after cutover, how post-cutover writes would be retained
or reconciled, and the conditions for returning to the source. A connection-string
change alone is not evidence of a tested rollback. Keep risk_manageability unknown
when the required recovery evidence is absent. A service migration does not
automatically encrypt retained volumes/snapshots or fix application authentication.

## GenAI ai_options

Include each of the 12 IDs from [ai-options.json](../assets/ai-options.json) exactly once. This is a service-fit comparison within GenAI/AI, not additional modernization pathways. New entries are unassessed and unselected, with unknown readiness.

Fields: option_id, selected (boolean), status, use_case, rationale, resource_ids, finding_ids, prerequisites, blockers, validation, alternatives, risks, cost_assumptions, source_urls, source_checked_at and readiness. List fields contain strings. Source dates are actual YYYY-MM-DD review dates or null, never future dates.

Common readiness keys are business_fit, data, security, quality, operations, availability and cost, each confirmed/unknown/blocked. Trainium and Inferentia additionally require model_compatibility and benchmark. The helper computes adoption_ready; input cannot override it.

Candidate/conditional options require actual resource/finding references, validation and alternatives. Candidate requires every readiness gate confirmed, no blockers, checked sources/date and cost evidence explanation. Remaining checks imply conditional. Only candidate/conditional options may be selected.

GenAI retains its shared weighted score, but ranking additionally requires at least one selected option and **all selected options** adoption-ready. A verified Bedrock option cannot override unresolved authorization for a selected AgentCore option. These checks validate declared evidence and structure; the helper does not prove quality, security or cost. Follow the [AI guide](genai-ai-fit.md).

Legacy inputs without ai_options receive 12 unassessed entries without changing original files or prior scores/statuses. CLI input_notes and reports disclose the upgrade. Even a legacy candidate remains unranked until the detailed selected combination is verified. ai_option_catalog_version is 1.0. Explicit incomplete, duplicate or unknown IDs are errors; historical reports are not rewritten automatically.

## Observability and messaging

Modern DevOps observability_options contains opentelemetry, managed-grafana and managed-prometheus. Cloud Native messaging_options contains msk, sqs, eventbridge and sns. Fields match ai_options, with common readiness business_fit/security/operations/availability/cost and additional gates from [platform-options.json](../assets/platform-options.json). platform_option_catalog_version is 1.0.

Missing legacy fields receive unassessed/unselected options, disclosed in CLI input_notes and reports. Preserve original files. Selected options must all be adoption-ready to pass the existing pathway ranking gate. With no selected options, retain existing ranking rules: CI/CD improvements do not require Grafana, and service-boundary changes need not add messaging. Validate group membership, completeness, references and readiness; suitability itself remains evidence-based agent judgment.

## Legacy six-pathway compatibility

If exactly the original six pathways are present and only modern-devops is missing, append an unassessed Modern DevOps entry. Keep the original file/candidate values, null criteria and no rank for the new entry. Disclose the upgrade in JSON/Markdown/HTML. Any other missing, duplicate or unknown pathway is an error. Preserve historical six-pathway reports as historical records.
