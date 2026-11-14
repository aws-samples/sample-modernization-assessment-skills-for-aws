# Move to Modern DevOps

Assess the ability to validate and deliver changes, then use operating feedback to improve the next change. This applies while retaining serverless, container or VM architecture. Adopting a CI/CD product alone is not an outcome. This guide proposes improvements; it does not authorize pipeline execution, deployment or permission changes.

## Evidence and assessment

Create PASS/FAIL/WARN/UNKNOWN/NOT_APPLICABLE findings for distinct criteria. Separate configuration presence from tested behavior. Preserve starter findings and add checks to a derived evidence snapshot with sources, hashes and IDs.

| Area | Evidence | Questions |
|---|---|---|
| Change traceability | PRs/merges, review rules, commits and deployments | Which change reached which environment? |
| CI/testing | Definitions and actual unit/integration/contract/negative-test runs | Are critical risks tested and failed gates enforced? |
| Supply chain/permissions | Dependency/secret/IaC scans, artifact provenance, role/trust scope | Can provenance and deployment privileges be traced? |
| CD/artifact promotion | Digests/versions and environment promotion history | Is the verified artifact promoted, or rebuilt differently per environment? |
| Delivery/recovery | Progressive release, health/alarm gates, bake, rollback tests | Is impact bounded and failure detected/recovered? |
| IaC/environment consistency | Templates, actual configuration, drift, config/secret handling | Do manual changes or environment differences break reproducibility? |
| Operating feedback | SLOs, alarm routing, runbooks, incidents, owners | Are failures turned into improvements? |
| Developer experience | Reproducible environments, queues, retries, flaky tests, interventions | Which repeated tasks and waits cause toil? |

CloudFormation, Git or alarm presence does not prove delivery works. An AWS inventory without a pipeline does not prove CI/CD is absent: it may use external tools. Without definitions/history, leave the area UNKNOWN and connect only observed opportunities conditionally. Do not invent resource IDs; candidates lacking required references remain not_assessed.

## Compare existing tools first

Use [observability guidance](observability.md) for OTel/ADOT, Managed Grafana and Managed Prometheus. Reuse shared findings/cost rather than counting them twice.

Check the current toolchain, capability, account/network boundaries and integrations. Compare improving GitHub Actions/GitLab CI/Jenkins or other current tools with suitable alternatives such as CodePipeline/CodeBuild. Do not require switching CDK/CloudFormation/Terraform. Verify current service availability, support and authentication integration before recommending products.

Consider promoting immutable, verified artifacts through automated test/health gates in small changes. Assess manual approvals against actual organizational requirements and risk reduction; more approval steps do not imply maturity. Include schema/data/config backward compatibility in rollback planning. For irreversible changes, define recovery or roll-forward conditions. Scale recommendations to development/synthetic-data environments rather than mechanically imposing a production pipeline.

## Metrics and scoring

State period, workload/environment, definition, sample size and source. A jointly selected recent 28-day window is an example, not a mandatory default.

| Metric | Definition to record |
|---|---|
| Deployment frequency | Successful deployments to the chosen environment over the period |
| Change lead time | Chosen start event, commit or merge, through successful deployment |
| Change failure rate | Deployments meeting an agreed failure definition / total observed deployments |
| Failed-deployment recovery time | Deployment-related incident start through recovery |
| Pipeline bottleneck | Queue and execution time by build/test/deploy/bake stage |
| Toil/flaky tests | Manual interventions, retry reasons, flakiness definition and sample |

Do not compare incompatible definitions. Zero samples leave ratios/times unassessed. Do not invent DevOps/DORA maturity tiers or improvement percentages. Without baselines, propose measurement before estimating saved effort or ROI.

Apply the six shared weights to verified business delay/impact, technical compatibility, operating ownership, failure/rollback risk, incremental delivery feasibility and measured runner/build/storage/security/operations cost. Unknown criteria remain null; do not relax ranking rules.

## Reports, diagrams and roadmap

Connect bottleneck to finding/evidence, improvement, validation, owner and rollback. Reuse IDs and avoid double-counting across Cloud Native and other pathways. When relevant, diagram source, build/test, artifacts, promotion and feedback separately from runtime traffic. Distinguish observed and proposed stages; never invent an existing pipeline. Follow the D2-first AWS redraw workflow.

Order waves around observed bottlenecks: establish measurement/traceability, improve gates, validate promotion/recovery, then improve operating feedback. Each wave needs an executable acceptance criterion and owner. Numeric targets/dates require user goals or measured evidence.

## Sources

Reviewed through AWS Knowledge MCP on 2026-10-20; recheck relevant definitions/features during execution.

- https://docs.aws.amazon.com/wellarchitected/latest/devops-guidance/dl.cd.6-refine-delivery-pipelines-using-metrics-for-continuous-improvement.html
- https://docs.aws.amazon.com/wellarchitected/latest/devops-guidance/metrics-for-continuous-delivery.html
- https://aws.amazon.com/blogs/devops/de-risk-releases-with-aws-codepipeline-rollbacks/
