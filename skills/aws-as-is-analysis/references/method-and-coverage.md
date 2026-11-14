# Method and collection coverage

Reference: sample-ai-driven-modernization-with-kiro, commit 1c3dd302aaf753ad2f39e239398245ee0d0a26eb, reviewed 2026-10-16. The infrastructure, application and database prompts informed the separation of actual CLI configuration, supplied code and authorized database metadata.

Use input validation, a scoped plan, actual collection, infrastructure/application/database analysis and a structured handoff. Do not replicate unnecessary approval pauses. Partial AWS-only analysis remains useful when missing code and database evidence are explicit.

| Area | Automated metadata | Requires additional evidence |
|---|---|---|
| EC2/VPC | VPCs, subnets, routes, IGW/NAT, SGs, instances, EBS, endpoints | OS/processes/packages, actual traffic/utilization |
| ELBv2 | Load balancers, target groups, listeners, registered targets | Actual request paths and application SLOs |
| RDS | Instance and cluster configuration | Schema, SQL compatibility, recovery tests |
| ECS/EKS | Cluster/service or cluster configuration | Image/code analysis and Kubernetes internals |
| Lambda | Selected runtime/memory/timeout/VPC metadata | Code, environment values, event behavior |
| S3 | Location, BPA, versioning, encryption and account BPA | Objects, data classification and actual public reachability |
| DynamoDB | Tables and PITR | Items, access patterns and load |
| Operations | CloudWatch alarms, log retention, CloudTrail configuration/logging | Procedures and incident/recovery exercises |
| IAM | Account summary | Central root management and full permissions design |
| Analytics | Optional Redshift clusters/OpenSearch domains | Queries, data models and processing internals |
| Observability | Optional AMP/Grafana workspace metadata | OTel instrumentation/propagation, dashboards/PromQL/telemetry bodies |
| Kafka | Optional MSK cluster metadata | Topics, consumers, connectors, lag and measured performance |
| Messaging | Optional SQS queue/SNS topic attributes | Message contents, consumer success/idempotency, SNS subscriptions |
| EventBridge | Optional buses/rules, target ARN/retry/DLQ | Input/transformers, Pipes/Scheduler/archive and actual delivery/replay |

This is a finite starter collector, not complete AWS or Well-Architected coverage. Verify official CLI operations before adding services and preserve the same evidence contract. Get operations are not inherently safe: secret retrieval, decrypted parameters, S3 objects, Lambda code downloads, ECR login tokens and database queries are not default inventory operations. Scope any cost/metric queries by period and applicable charges.

## Host and network interpretation

For authorized server investigation, use the [SSH/SSM extension](host-inspection.md). It is separate from default collection and requires supported Linux/Python, connectivity and existing authentication. Preserve inaccessible fields as unknown. Manifest/Git metadata describes deployed trees, not necessarily loaded code.

Subnets, SG attachments and registered targets establish configuration relationships. Public SSH ingress alone does not prove reachability without routing, IGW, public IP and NACL evidence. A shared VPC does not prove EC2-to-RDS traffic. A DB subnet group's AZ count is not actual node placement.

Apply optional workload/tag filtering after inventory collection while retaining shared network/security/LB dependencies. CloudTrail shadow/organization trails may be owned elsewhere; do not expand regions/accounts automatically and mark unavailable logging checks unknown.

## Sources

- https://github.com/aws-samples/sample-ai-driven-modernization-with-kiro
- https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-pagination.html
- https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-instances.html
- https://docs.aws.amazon.com/cli/latest/reference/rds/describe-db-instances.html
- https://docs.aws.amazon.com/cli/latest/reference/elbv2/describe-load-balancers.html
