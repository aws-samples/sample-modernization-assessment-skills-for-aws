# Pathway decision criteria

The seven modernization directions and the two foundations are this package's comparison framework, not an official AWS maturity score or mandatory sequence. Multiple pathways can apply to one workload. The report leads with the seven directions in the order below; Modern DevOps and Cloud Native are foundations, the delivery and operations practices that make the moves safe, shown after the directions and not counted among them.

| Pathway | Fit signals | Key constraints/evidence | Options and alternatives |
|---|---|---|---|
| Container | Packageable processes/dependencies; consistent deployment or isolation | State, sidecars/cron, OS/native dependencies, image supply chain, graceful shutdown | ECS/Fargate; EKS when Kubernetes requirements and operating capability justify it; improve existing EC2 |
| Serverless | Event/request units, variable load, managed integration | Execution/state/memory/concurrency, cold starts, idempotency, database connections, quotas | Lambda/API Gateway/EventBridge/SQS/Step Functions; compare container tasks |
| Managed Services | Self-managed software or hand-run operations on EC2 that an AWS managed service can take over: a cache, broker, search engine, scheduler or self-run cluster on instances, a bastion host for administration | What the host runs (tags, UserData, templates, host evidence), state and data it keeps, versions and plugins, who operates it, access paths | ElastiCache/MemoryDB, Amazon MQ, MSK, OpenSearch Service, Systems Manager Session Manager instead of a bastion, Fargate for a single application host; keep and improve the host when no managed equivalent fits |
| Managed Database | Self-managed backup/patch/HA burden with compatible access patterns | Engine/extensions/procedures, CDC, verification, RTO/RPO, licensing | RDS/Aurora or purpose-fit DynamoDB; migration tooling only after compatibility/cutover analysis |
| Managed Analytics | Growing pipelines/data, batch/stream consumers, operational burden | Classification, lineage, freshness, queries, governance, scan/storage cost | Workload-appropriate S3/Glue/Athena/Redshift/EMR Serverless/OpenSearch |
| GenAI | Verified AI workflow, measurable quality and data rights | Task, security, quality, operations, availability, cost; accelerator compatibility/benchmarks | Apply the [12-option AI guide](genai-ai-fit.md) |
| Open Source | License-included engines and operating systems (COMMERCIAL_LICENSE_DEPENDENCY findings: Oracle, SQL Server, Windows Server) | SQL dialect, stored procedures, drivers and tools, .NET Framework or Windows-only dependencies, licensing terms, conversion and test effort | Aurora/RDS PostgreSQL or MySQL with DMS Schema Conversion and DMS, Babelfish for Aurora PostgreSQL for SQL Server applications, Linux and .NET for Windows hosts; keep the licensed engine when compatibility or terms decide it |
| Cloud Native | Service-boundary, scaling, resilience or managed-integration improvements | API/event contracts, state, failure isolation, platform capability | Improve current structure or use incremental strangler migration; do not mandate microservices |
| Modern DevOps | Manual delivery, long waits, flaky tests, environment drift, slow recovery | Change traceability, gates, artifacts, IaC, progressive delivery/rollback, ownership, metrics | Improve existing CI/CD or compare suitable AWS/external tools using the [DevOps guide](modern-devops.md) |

Cloud Native includes [MSK/SQS/EventBridge/SNS](message-event-driven.md) options; Modern DevOps includes [OTel/Managed Grafana/Managed Prometheus](observability.md). These do not add top-level pathways. Managed Services moves software off hosts the team runs; Managed Database covers the data stores themselves, so a self-managed database on EC2 belongs there, and Open Source covers the license-included engines and operating systems. Other pathways can reuse the same findings and evidence.

## Shared interpretation

Security, observability and recovery gaps are often shared prerequisites, not automatically solved by changing services. Distinguish rehost/replatform/refactor/retain/retire strategies from the service pathways. Do not select EKS, DynamoDB or GenAI merely because they seem newer.

Verify current regional support and quotas. Without cost evidence, do not assert savings, ROI or payback; include development, transfer and parallel-operation costs. Waves need entry/acceptance conditions, rollback and owners rather than arbitrary calendar dates.

Cloud Native primarily addresses runtime structure; Modern DevOps addresses change delivery and operating feedback. Reuse finding IDs for shared tasks and never double-count cost, effort or benefits.

## Public sources

- https://aws.amazon.com/modernizing/
- https://docs.aws.amazon.com/decision-guides/latest/containers-on-aws-how-to-choose/containers-on-aws-how-to-choose.html
- https://docs.aws.amazon.com/lambda/latest/dg/welcome.html
- https://docs.aws.amazon.com/prescriptive-guidance/latest/large-migration-guide/migration-strategies.html
- https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html
- https://docs.aws.amazon.com/prescriptive-guidance/latest/strategy-database-migration/welcome.html
- https://docs.aws.amazon.com/decision-guides/latest/analytics-on-aws-how-to-choose/analytics-on-aws-how-to-choose.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html
- https://docs.aws.amazon.com/dms/latest/userguide/CHAP_SchemaConversion.html
- https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/babelfish.html
- https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html
- https://docs.aws.amazon.com/prescriptive-guidance/latest/application-portfolio-assessment-guide/overview.html
- https://docs.aws.amazon.com/wellarchitected/latest/devops-guidance/dl.cd.6-refine-delivery-pipelines-using-metrics-for-continuous-improvement.html
