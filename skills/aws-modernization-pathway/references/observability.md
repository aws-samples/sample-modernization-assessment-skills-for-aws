# Observability suitability

Use Modern DevOps observability_options to compare OpenTelemetry/ADOT, Amazon Managed Grafana and Amazon Managed Service for Prometheus. Include retaining CloudWatch/X-Ray, self-managed and external tools. Start with an unobservable failure, latency or capacity problem rather than a missing product.

| Option | Fit signals | Verification | Reasons to defer |
|---|---|---|---|
| OTel/ADOT | Distributed tracing, instrumentation standards, backend portability | SDK/Collector/exporter compatibility, propagation, sampling, failures | Current instrumentation is sufficient |
| Managed Grafana | Multi-source operating views and Grafana operating burden | SSO, data-source access, network/plugins, alarm ownership | Existing dashboards meet needs |
| Managed Prometheus | Prometheus/PromQL requirements and backend burden | Scrape/remote-write, HA deduplication, cardinality, queries | No Prometheus need or economic evidence |

OTel instruments/collects/transports signals; it is not the storage backend. Grafana visualizes/explores/alerts; Prometheus focuses on metrics. Installing all three does not complete metrics/logs/traces. ADOT and upstream OTel may support different versions/components. AMP and AMG need not always be adopted together.

## Evidence

Inspect authorized instrumentation libraries, Collector configuration, context propagation, exporters and deployment. Compare SLOs/user journeys with dashboards, alarms, recipients and runbooks. Record ingestion delay/drop/errors, active series/label cardinality, sample rate, queries, retention and period. Check for sensitive/high-cardinality labels; read telemetry bodies only when authorized.

Optional --services amp,grafana collects workspace metadata. Absence does not prove OTel/external tools are absent. Configuration alone does not verify propagation or ingestion. The collector does not query dashboards, PromQL, log/trace bodies or API tokens.

## Readiness and proposal

For OTel, verify representative cross-service signals, privacy filtering, sampling/storage budgets and application impact/loss/fallback during Collector failure, retries or queue saturation. Its extra gates are telemetry_compatibility and pipeline_resilience.

For Grafana, test user/tenant data-source access, SSO/network connectivity and useful operating views. Resolve overlapping Grafana/AMP/CloudWatch alerts and ownership. Gates: data_source_access and alert_ownership.

For AMP, verify remote-write authentication, Prometheus compatibility, HA deduplication, ingestion/query load and cardinality growth. Compare measured usage with current retention, quotas and rates. Gates: telemetry_compatibility and cardinality_capacity.

Diagram signal, SDK/Collector, backend, dashboards/alarms and response ownership. Separate logs/traces from metrics and observed/proposed account/network boundaries. For small serverless workloads, compare first filling existing CloudWatch/log/request-ID gaps. Share finding IDs and do not double-count cost or benefits across pathways.

## Sources

Reviewed 2026-10-20. Recheck components, authentication, regions, limits and pricing during assessment.

- https://opentelemetry.io/docs/what-is-opentelemetry/
- https://docs.aws.amazon.com/grafana/latest/userguide/what-is-Amazon-Managed-Service-Grafana.html
- https://docs.aws.amazon.com/prometheus/latest/userguide/what-is-Amazon-Managed-Service-Prometheus.html
