# Assessment method

The starter helper checks public administrative SG ports, IMDSv2, EBS encryption/attachment, RDS public access/encryption/backups/resilience, S3 BPA, DynamoDB PITR, CloudTrail logging and log retention; `workload_rules.py` adds the workload checks (TLS listeners, IAM wildcards, alarm targets, ECR/ECS/EKS settings, ElastiCache, Aurora, secrets rotation, Bedrock governance, autoscaling, backup plans, CI gates, Graviton, observability); `platform_rules.py` (2.1.5) adds account posture, lifecycle and edge rules listed in SKILL.md step 3. Configuration alone does not establish performance, complete cost/sustainability posture, recovery-test success or operating procedures.

| Pillar | Assess | Additional evidence |
|---|---|---|
| Operational Excellence | Ownership, observability, change management, runbooks, incident response | CI/CD, interviews, incident history |
| Security | Identity, least privilege, boundaries, encryption, detection, data protection | Policies, access paths, classification, controls |
| Reliability | AZ dependencies, isolation, backups/restores, RTO/RPO | Restore/failover/DR exercises |
| Performance Efficiency | SLOs, workload behavior, measurement, caching/async choices | Time-bounded metrics and load tests |
| Cost Optimization | Usage, idle resources, rightsizing, rates/commitments, operations | Billing, tags and staffing assumptions |
| Sustainability | Demand-relative utilization, schedules and data lifecycle | Utilization/demand and retention evidence |

Show all six pillars; unknowns remain UNKNOWN and unevaluated scores null. Tie severity to actual use and business importance. Do not assign identical risk mechanically to a disposable development database and a critical production database.

Use the [Modern DevOps](../../aws-modernization-pathway/references/modern-devops.md), [AI fit](../../aws-modernization-pathway/references/genai-ai-fit.md), [observability](../../aws-modernization-pathway/references/observability.md) and [messaging](../../aws-modernization-pathway/references/message-event-driven.md) references for evidence-backed supplemental findings. Workspace/queue/model/pipeline presence alone proves neither adoption value nor successful operation. Keep these findings within the existing six pillars.

## Findings contract

Each checks entry has id, rule_id, pillar, resource_id (null for workload/scope questions), status, severity, weight, evidence_ids, observation, recommendation, source_urls and confidence; account-level checks (2.1.6) also carry `enablement` (enabled, partial, not_enabled, not_in_plan, unknown, observed) and owner questions carry `question`. Status is PASS/FAIL/WARN/UNKNOWN/NOT_APPLICABLE. Severity is HIGH/MEDIUM/LOW/INFO, a local priority rather than official HRI/MRI. Use the [evidence levels](../../aws-as-is-analysis/references/workload-evidence.md): configuration, template_declared, source_declared, runtime_observed, measured, documented, inferred or unverified. Put the qualifier in the observation itself; a closing disclaimer is insufficient.

For manual findings, preserve automatic results and create a separate input snapshot containing additional evidence under the same checksum contract. Recalculate with `--checks FILE`. Do not invent a PASS without evidence. Each (rule_id, resource_id) pair represents one check; split different questions into different rule IDs. Use `assessment_reason` to explain a changed baseline judgment, severity, weight or pillar. Explain removals/workload exclusions in the report. Do not silently tune weights to obtain a more plausible score.

Use a host-derived snapshot for [SSH/SSM evidence](../../aws-as-is-analysis/references/host-inspection.md). Deployed package/dependency/path metadata is a clue, not proof of vulnerable versions, compatibility, database schemas or traffic. Preserve host partial coverage and remote-execution side effects.

## Local scoring

PASS=1, WARN=0.5, FAIL=0. Pillar score is 100 times the weighted judged value divided by judged weight. No judged weight yields null, not zero or 100. Evidence coverage is judged count divided by judged plus UNKNOWN count; exclude NOT_APPLICABLE from both denominators. Weight must be an integer from 1 to 5.

Checks are resource-level, so services with many resources can dominate a pillar. Show pillar scores rather than a misleading total. Neither UNKNOWN nor successful API collection is proof of architecture quality.

The helper emits `checks.baseline.json`, `scorecard.baseline.json` and
`assessment-basis.json` alongside the final scorecard. Baseline scores are the
automatic subset, not a second overall health rating. The basis records rule-set
version, check/scope fingerprint, additions, removals and modified scoring fields.
It detects structural differences, not the truth of human judgments.

Compare scores only with aligned workload scope, rules, weights and interpretation.
A matching fingerprint is necessary, not sufficient: changed manual rule semantics
also need disclosure. If scope or checks differ, explain the difference rather than
claiming improvement/deterioration. Do not leave excluded tooling in the workload
score without explicitly showing that inclusion.

## Qualitative grades

Readers see a grade per pillar before any number. The grade is a fixed reading of the judged
checks and can always be traced to the findings behind it (the report shows the driving rules in
the tile). First matching rule wins:

| Grade | Rule | Meaning for the reader |
|---|---|---|
| Fix first (`critical`) | at least one FAIL with HIGH severity | A high-severity item exists; worth settling before any modernization step |
| Needs work (`weak`) | any FAIL, or three or more WARN | Failures or several warnings; worth planning in the first wave |
| Watch | one or two WARN, or any UNKNOWN item | Minor warnings or open questions the configuration cannot answer; confirm with evidence |
| Sound | every judged check PASS (at least one judged) and no UNKNOWN item | Keep as is and re-check on change |
| Unassessed | nothing judged and nothing unknown | No judged evidence yet |

Grades are not AWS Well-Architected Tool ratings and do not replace the numeric local score,
which stays in the method appendix and feeds pathway ranking. `report_kit.grade_pillar`
implements the rubric; keep the table and the code in step.

## Interpretation cautions

Evaluate protocol, port and public IPv4/IPv6 ranges within the same SG permission; ICMP values are not TCP ports. PubliclyAccessible is a configuration signal, not a connectivity test. Aurora MultiAZ=false alone does not prove poor resilience; inspect cluster members and failover placement.

Combine account and bucket S3 BPA per flag; public policy/ACL analysis remains separate. CloudTrail presence does not establish logging health without status evidence. Unattached EBS is an optimization candidate, not authorization to delete. Enabled backups do not prove a successful restore.

An empty service list does not establish absence of external monitoring, backups,
CI/CD or manual recovery procedures. Judge the observed configuration check and
keep the broader operating capability UNKNOWN until supported. Template-defined
database engines/backups describe intended deployment. Confirm live configuration
or keep the runtime question open. Production-oriented severity for a workshop or
development baseline must be labeled as an explicit assessment assumption.

### "Not enabled" answers are judgements, not gaps

Some APIs answer with an error that is itself the finding: `InvalidAccessException` from Security Hub means the service is off, `AWSOrganizationsNotInUseException` means a standalone account, `SubscriptionRequiredException` from the support or Health API means a Basic/Developer support plan. The collector records these as `status: not_enabled`; `score()` accepts them as judged evidence. The rules never turn an account-wide service that is off into FAIL: Security Hub, GuardDuty, Config, Inspector, Access Analyzer, Compute Optimizer and Organizations membership are WARN with an `enablement` state (`enabled`, `partial`, `not_enabled`, `not_in_plan`, `unknown`, `observed`) and a recommendation phrased as a suggestion: what the service does, what enabling it would add for this workload (its own EC2/ECR/Lambda/S3 counts, whether GuardDuty already runs, the IaC share), and when leaving it off is a reasonable choice. The customer may run another tool, have no compliance obligation or have chosen not to pay for it; the report explains and proposes, it does not order. A missing support plan is an owner question (Trusted Advisor is a benefit of the plan, not a control the workload failed). Denied or failed calls stay UNKNOWN. The assessment report renders these states in its account-level table (Enabled / Partially enabled / Not enabled / Not in support plan / Could not read / Observed, plus a `Consider` tag) and adds a card per service worth considering with three parts: what it is, what enabling adds, and the workload-specific suggestion.

### Documented empty answers are judgements, not gaps

Some APIs answer "nothing is attached" with an empty body and exit code 0.
`wafv2 get-web-acl-for-resource` is the known case: an empty response means no Web
ACL is associated with the resource. The supplementary-evidence merger keeps an
allow-list of such operations (`EMPTY_MEANS_ABSENT`); for them the collector records
the payload as `{}` and the merger marks the record `empty_response_meaning`. The
`SEC_WAF` rule then judges an internet-facing application load balancer WARN (no Web
ACL), PASS (a Web ACL is present) or UNKNOWN (no WAF evidence collected or the query
was denied). Every other operation keeps the rule "never manufacture a successful
empty result from blank stdout". Field case: 2026-10-30, the empty WAF answer was
recorded as `validation_error` and the check stayed UNKNOWN; this is the fix.

`findings.json` records `as_is_snapshot` (path, resource count, inventory hash) so
the unified report reads the same snapshot the review judged. When host or
supplementary evidence has been merged into a later `03-as-is-*` directory, point
`review.py --as-is` at that directory; `assessment_report.py` follows the pointer and
fails if it is missing rather than silently reading another snapshot.

## HTML report

`review.py` renders `review.html` from findings, scorecard, inventory and manifest with the
shared `scripts/report_kit.py` (standard library only, inline SVG, no JavaScript, no external
fonts or assets). Sections: hero and KPI tiles, local pillar-score bars with evidence coverage,
judgement donut and per-pillar stacked bars, fail/warn by severity, the final AS-IS diagram or
a draft notice, inventory-by-type bars with collection coverage, findings grouped per pillar
(fail/warn/unknown first, passed checks collapsed) and scope limits. Every number comes from the
JSON artifacts; the chart kit never infers judgements. Status colours mark PASS/WARN/FAIL/UNKNOWN
and the single orange accent marks the lowest judged pillar. The English draft is the canonical
source; localize it with the output-language workflow instead of editing the helper output.
Keep `report_kit.py` identical in both skills (tests enforce this). With `--run-metadata`, the report adds a Run provenance section (LLM model, total inference time, total tokens) from the agent-declared `run-metadata.json`; see the aws-mcp-setup [run metadata](../../aws-mcp-setup/references/run-metadata.md) reference.

https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html
