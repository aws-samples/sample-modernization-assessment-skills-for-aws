---
name: aws-as-is-analysis
description: Assess AWS current state using scoped inventory, workload-linked deployment templates and source evidence. When host inspection is requested, inspect named Linux hosts through SSH/SSM and distinguish declared, deployed and running applications.
---

# AWS current-state analysis

Stage **3 of 5**. Apply the [method and coverage](references/method-and-coverage.md), separating observed configuration from unverified application and database internals. Collect actual evidence before drawing conclusions.

Keep this skill in English. Follow the shared [output-language policy](../aws-mcp-setup/references/output-language.md) and write current-state/host explanations in the resolved request language. English helper drafts are intermediate; raw evidence, IDs and machine fields remain canonical.

## Inputs and scope

- Access and toolkit checks from stages 1/2.
- `$RUN_DIR/scope.json` from aws-cli-setup: account, auth mode/profile, regions, services, workload filter (stack names, tags, excluded stacks) and host inspection choice. Read it; do not ask the user again. If it is missing, ask the user to run aws-cli-setup first rather than inventing a scope.
- Supplied or discoverable workload-linked source repositories, deployment documents, SLO/RTO/RPO and database metadata.
- For host inspection: instance ID or host, existing SSH user/trusted host key or SSM access, and permitted application roots. Remote Linux/Python 3.9+ is required.

Collect one account per snapshot. For multiple accounts, repeat into separate directories and preserve account identity. Organization-wide discovery, all-region scanning, SSH/SSM execution, SQL and application data reads are outside the default scope. Expand only to the requested scope.

## Workflow

1. Reconcile account and regions with the user request and prior checks. Write a short local `collection-plan.md`; do not introduce a separate document-approval pause.
2. Check supported services with `scripts/collect.py --help`, then run:

   ```bash
   python3 "$SKILL_DIR/scripts/collect.py"      --profile "$PROFILE" --expected-account "$ACCOUNT"      --regions ap-northeast-2      --max-api-calls 500 --max-detail-resources 100 --max-workers 8      --output "$RUN_DIR/03-as-is"
   ```

   For environment credentials, replace `--profile NAME` with `--environment`. `--plan-only` makes no AWS requests. Keep automatic pagination; do not truncate with `--no-paginate`/`--max-items` and call the result complete. The budget counts logical CLI invocations, not individual HTTP pagination/retry requests.

   Without `--services`, the collector scans the default set (`collect.py --help`): the eleven workload services (EC2 incl. VPC/TGW/VPN/flow logs/EIPs, Classic and v2 load balancers, Auto Scaling, RDS, ECS, EKS, Lambda, S3, DynamoDB, CloudWatch/Logs, CloudTrail, IAM), SSM managed-instance OS info, the network edge (ACM, API Gateway, WAF, Direct Connect, Route 53, CloudFront) and account posture (Security Hub, GuardDuty, Config, Inspector, IAM Access Analyzer, Compute Optimizer, Trusted Advisor, Health, Organizations). **Discovery runs first**: one Resource Groups Tagging API call per region lists every tagged service (ARNs and tag keys only) and automatically enables the optional listings for services it finds (ElastiCache, ECR, SQS, SNS, EventBridge, MSK, OpenSearch, Redshift, Glue, Athena, Kinesis, SageMaker, Bedrock, Step Functions, Cognito, MQ, IoT, DMS, MGN and others in `OPTIONAL`). Add `--services` only to narrow the scan or to force a service discovery cannot see (never-tagged resources). Trusted Advisor and AWS Health need a Business or higher support plan; on Basic/Developer the calls are recorded as `not_enabled`, which does not lower the collection status. Collection covers configuration metadata, not OTel instrumentation, telemetry bodies, Kafka internals or message payloads.
3. Account mismatch or expired identity stops collection. Preserve per-service permission errors/timeouts and continue permitted queries. Budget/detail limits produce `partial`. Use a fresh directory for every snapshot.
4. Read the [data contract](references/data-contract.md), inventory and manifest. Distinguish successful empty results, failed queries and excluded scope.
5. For a workload assessment, run the workload enrichment helper. It reads `scope.json`, follows the scoped stacks to their templates and resources, resolves the workload scope (stack physical IDs, scope tags, and everything inside an in-scope VPC, which picks up load balancers or nodes created outside CloudFormation), and collects the type-driven metadata the review needs: listener rules and attributes, WAF association, task definitions and placement, EKS node groups/add-ons/access entries, ElastiCache, secret rotation metadata, ECR settings, workload IAM role policy documents, pipelines/builds, alarm subscriptions, Bedrock invocation logging and guardrails, and up to `--source-files` pinned CodeCommit files. Every call is an allowlisted describe/list/get; values under sensitive keys and inline credentials are redacted before writing. Then merge:

   ```bash
   python3 "$SKILL_DIR/scripts/supplement.py" --as-is "$RUN_DIR/03-as-is" --scope "$RUN_DIR/scope.json" \
       --profile "$PROFILE" --output "$RUN_DIR/03-supplementary"
   python3 "$SKILL_DIR/scripts/merge_supplementary_evidence.py" --as-is "$RUN_DIR/03-as-is" \
       --supplement-dir "$RUN_DIR/03-supplementary" --output "$RUN_DIR/03-as-is-enriched"
   ```

   The enriched snapshot carries `scope-resources.json`, which the review and the diagram audit use. Do not write per-run collection scripts for what this helper covers; if a material question needs another read-only call, add it as a supplementary record in the same format and say why. Then follow [workload evidence enrichment](references/workload-evidence.md) before declaring application, database or delivery evidence unavailable. Trace scoped resources to their stacks, bootstrap/deployment definitions and repositories. Inspect relevant authorized evidence; record denied, unavailable, excluded and budget-limited branches of discovery. An inventory-only request may stop after inventory.
6. Write `as-is-report.md` using the [template](assets/as-is-report.template.md). Include a workload map and evidence/gap table. Separate API-observed configuration, template/source declarations and verified runtime observations **in each claim**, not only in a closing disclaimer. Configuration relationships do not prove traffic, reachability or tested failure behavior. An EC2 tag does not establish its application or database engine.

   Record authorized CI/CD definitions/history, tests, artifact/deployment/rollback evidence and IaC drift with sources and observation periods. Stack presence does not prove automated delivery.

   For AI fit, record actual document/image/audio/text workflows, data rights/languages/classification, models/agents, training/inference load and quality/latency/cost baselines. Do not send source/data/models to external services or execute inference, training or agents during default collection.
7. When host investigation is requested, follow [SSH/SSM inspection](references/host-inspection.md). Plan with `inspect_host.py`, then use `--execute` for the authorized named host. Compare processes, services, containers and explicitly scoped manifest/Git metadata. Do not bypass host-key checks, broaden permissions or install an agent automatically.
8. Merge verified EC2 host evidence into a fresh derived snapshot with `merge_host_evidence.py`. Preserve originals and pass the derived path to review. A deployed revision does not prove code loaded by a process. Read source/database internals only within an existing authorized scope; never dump secrets or user data.

## Handoff

Pass the latest verified derived inventory/manifest, raw evidence, workload map, enrichment outcomes and current-state report to **aws-architecture-review**. Use `merge_supplementary_evidence.py` for collected deployment/source supplements and `merge_host_evidence.py` for executed host probes. Keep originals local, excluded from Git and external MCP requests. A completed helper run alone does not complete a workload assessment.

Collector exit codes: `0` complete within scope, `2` partial, `1` identity/execution failure. Partial evidence can support review, but gaps must not become PASS findings.
