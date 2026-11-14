# Stage 3 to 4 to 5 data contract v1.0

A run directory contains one assessment's related files. Never overwrite a collected snapshot or mix collection times by reusing it.

```text
RUN_DIR/
  01-setup/access-check.json
  02-toolkit/toolkit-check.json
  03-as-is/{collection-manifest.json,inventory.json,raw/,as-is-report.md}
  04-review/{findings.json,scorecard.json,review.md,review.html,diagrams/}
  05-modernization/{pathways.json,recommendations.md,recommendations.html,diagrams/}
```

## Collection manifest

Preserve schema_version, run_id, account_id, partition, regions, services, started_at and completed_at. Status is complete, partial or failed.

Each calls entry has id, service, operation, region, non-secret arguments, status, error_code, collected_at, raw_path and sha256. Query states are ok, access_denied, expired_token, not_found, throttled, error and skipped. Limits, truncations and coverage document actual results; the configured budget is not total AWS service coverage.

budget_unit is aws_cli_invocations. api_calls_executed and max-api-calls count logical CLI invocations, not HTTP pagination/retry requests. Calls run concurrently, at most max-workers (default 8) AWS CLI processes at once; `--max-workers 1` is sequential and yields the same resources and relationships. Half of max-api-calls is reserved in equal shares per requested service (limits.reserved_calls_per_service) and the rest is shared; a finished service releases its unused share. A service that exceeds its share while the shared half is spent records `skipped` / `api_budget_exhausted` calls and the run is `partial`, so a large service (for example many S3 buckets) cannot silently drop the services after it. api_calls_by_service shows the split. Evidence ids are allocated in call order and stay unique; their order across services is not fixed between runs.

For deployment/source enrichment, follow [workload evidence](workload-evidence.md).
`merge_supplementary_evidence.py` preserves the base manifest and adds separately
counted records with evidence_kind, provenance, response_complete and resource_ids.
The verified derived snapshot is the input for review. Successful files must be
nonblank; JSON metadata must parse as an object or array. Empty lists can establish
only API-scoped absence when the response is complete and its collection succeeded.

## Inventory

Preserve schema/run/account/region/service identity and collection_status. Each resource has id (account + region + type + native ID), type, native_id, name, account_id, region, attributes and evidence_ids. Separate observations hold status, data and evidence_id for detail queries such as S3 settings, PITR and trail logging.

Relationships contain source, target, relation, evidence_ids and certainty=observed_configuration. The scope_note records boundaries and limitations.

## Discovery, account-level records and coverage (2.1.5)

Since 2.1.9 the account services include `ce` (Cost Explorer): one `ce.cost-by-service` record whose attributes summarise `get-cost-and-usage` for the last three closed months (`months[].total_usd`, `months[].services[]` top 12 by unblended cost, `scope: account`). `DataUnavailableException` is recorded as `not_enabled`. The figures are the account's bill, not the workload's share.

- `calls[].status` gains `not_enabled`: the API answered that the account has not enabled the service or lacks the support plan (`SubscriptionRequiredException`, `InvalidAccessException`, `AWSOrganizationsNotInUseException`, `OptInRequired`). It is a definitive observation, is judged by the review like a successful call, and does not make the snapshot `partial`. `coverage.not_enabled` counts them.
- `resources[].level` is `workload` or `account`. Account-level records (`discovery.tagged-resources`, `securityhub.hub`, `guardduty.detector`, `config.recorder`, `inspector2.account-status`, `accessanalyzer.analyzer`, `computeoptimizer.enrollment`, `support.trusted-advisor`, `health.events`, `organizations.organization`, plus the older `iam.account-summary`, `s3.account-public-access-block`, `cloudtrail.trail`) carry `collection: {status, error_code}` for the call that produced them, stay out of the workload scope and the diagram, and are judged by `platform_rules.py`.
- `manifest.discovery[region].tagging` summarises the Resource Groups Tagging API scan: `services` (ARN service name → count), `resource_types`, `total`, `cloudformation_managed` (resources with `aws:cloudformation:*` tag keys). Only resources that are or were tagged appear. `manifest.discovery[region].config_resource_counts` is added when AWS Config is recording.
- `manifest.auto_enabled_services` lists optional listings that discovery switched on; `manifest.service_coverage[service]` records `discovered_resources`, the collector used and a status: `collected`, `supplement_when_in_scope`, `not_collected`, `not_enabled`, `denied`, `skipped`. The report shows this table so a service that exists but was not assessed is visible.
- Default `--max-api-calls` is 500; discovery costs one call per region and each optional listing one call per region.

## Validation

Resource and evidence IDs must be unique. Relationship endpoints and all references must exist. Validate raw SHA-256 hashes. Only a successful empty response establishes absence; interpret not_found per API. Partial collection must remain visible in reports. A configuration snapshot is not transactionally consistent across resources. Downstream artifacts reference originals instead of modifying them.

## Optional host evidence

03-host/HOST contains host-manifest.json, raw/h0001.json and host-report.md. Record transport, target, execution/verification status, probe SHA-256, timestamps, optional SSM command ID and evidence. States are planned, observed, partial, failed or unsupported. Planned means no connection was attempted; failed is not successful evidence. Preserve command ID and uncertain completion on timeout without resubmission.

merge_host_evidence.py verifies account/region/instance, raw checksum and schema, then creates a new snapshot. It adds EC2 observations.host_metadata, observed host.application roots and observed_on relationships. Keep partial-host limitations in truncations. Do not claim a deployed tree matches loaded code. Non-EC2, failed or planned-only host results are not merged by this helper.

The derived manifest has read_only=false to indicate remote execution evidence, separately from application_changes_requested=false. SSM/access history can be a side effect. api_calls_executed retains the original collector count; it does not count host transport calls. host_inspections_added counts added host evidence units.

## Workload scope (scope-resources.json)

`supplement.py` writes `scope-resources.json` next to its records, and `merge_supplementary_evidence.py` copies it into the derived snapshot. It lists every in-scope inventory resource with its reasons (`stack:<stack>/<logical id>`, `tag`, `in-scope-vpc:<vpc>`, `attached-to-scoped-instance`, `placement-context:<vpc>`, `account-level-governance`), the in-scope VPCs and the stack resource summaries (logical ID, physical ID, type). The review evaluates only these resources, and the diagram audit requires every one of them, plus architectural stack resources the inventory does not list, to be drawn.
