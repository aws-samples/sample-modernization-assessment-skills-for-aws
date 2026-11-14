# Message/event-driven suitability

Use Cloud Native messaging_options to compare MSK, SQS, EventBridge and SNS, including retaining synchronous APIs. Distinguish Kafka streams, work queues, event routing and fan-out; combine them only when needed.

## Workload evidence

Identify producers/consumers, commands versus facts/events, synchronous dependencies and failure propagation. Record bursts, throughput, payload sizes, latency, ordering scope, duplication, retention/replay, consumer count, transaction/consistency boundaries, schema evolution and classification. For Kafka, inspect authorized client/protocol, connector, topic/partition/group, lag and operating evidence. Broker presence or queue depth alone does not prove value.

| Option | Fit signals | Verification | Reasons to defer |
|---|---|---|---|
| MSK | Kafka compatibility, independent consumers, retained stream replay | Client/connector, partitions/keys, lag, broker/capacity and permissions | Simple queue requirements or no Kafka need |
| SQS | Decoupled work, buffering, independent workers | Standard/FIFO, visibility, retries/DLQ, idempotency | Immediate consistent synchronous response required |
| EventBridge | Domain/AWS/SaaS event routing | Schema/filter/target support, retry/DLQ, archive/replay | Treating it as a universal queue/Kafka replacement |
| SNS | Multi-subscriber fan-out | Topic/subscription/filtering and target-specific recovery | Assuming it alone provides all per-consumer retention/replay needs |

Compare current MSK Provisioned/Serverless capabilities, clients, quotas and cost. Distinguish EventBridge buses, Pipes and Scheduler. The documentation reviewed on 2026-10-20 differentiates Custom Event Bus and Custom Event Bus - Classic, including ordering, retention, replay and subscription behavior; do not apply historical rule/target limitations to every bus model.

Compare SNS-to-SQS when fan-out needs independent buffering. Consider other evidenced alternatives such as Kinesis for streaming without changing fixed catalog IDs arbitrarily.

## Delivery and consumer safety

Model duplicates and reordering, then verify actual guarantees by product, mode and target. SQS FIFO or Kafka transactions alone do not establish end-to-end exactly-once external database/payment effects. Define global/partition/message-group ordering and its parallelism/throughput trade-off.

Verify idempotency keys, conditional writes, deduplication lifetimes and races during retries/replay. Compare outbox/CDC approaches for database-change/event-publication gaps and inspect actual transaction boundaries.

Test retry/visibility/expiration/DLQ/redrive, poison messages, partial batch failures, consumer interruption/recovery and backpressure. Delivery to a target is not business completion. Connect queue age, lag, failures and DLQ alerts to business completion metrics. Do not assume uniform retry/DLQ/retention/replay across targets/modes. Define replay scope/rate, authorized boundaries, audit, duplicate effects, stop conditions and recovery ownership.

## Optional collection

--services kafka,sqs,events,sns collects MSK cluster, SQS queue and SNS topic attributes, and EventBridge buses/rules/target ARN/retry/DLQ metadata. Exclude target Input/InputTransformer and SNS subscription endpoints. Kafka internals, broker connectivity, Pipes/Scheduler/archive, SNS subscription detail and other bus models' subscription/retention configuration are outside this starter.

Do not receive messages: receiving may change visibility. Do not send/publish/put-events, delete/purge, redrive or replay. Failed queries remain UNKNOWN and distinct from successful empty results. Configuration is not automatically converted into fit or reliability scores.

## Assessment and roadmap

Add delivery_semantics, consumer_safety and replay_recovery gates; MSK also needs kafka_compatibility and partition_capacity. Unverified selected combinations remain unranked. Cloud Native improvements without eventification need not select a messaging product.

Diagram producer, bus/queue/stream, consumer and relevant DLQ/replay, contracts and permission boundaries, distinguishing observed/proposed. Explain changes to API responses, user waiting/status queries, compensation and remaining messages after rollback. Compare request/payload/retention/polling/broker/partition/transfer/operations cost; do not invent latency or savings improvements.

## Sources

Reviewed 2026-10-20; recheck mode-specific guarantees, limits, regions and rates.

- https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html
- https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html
- https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html
- https://docs.aws.amazon.com/sns/latest/dg/welcome.html
- https://docs.aws.amazon.com/decision-guides/latest/decision-guides/sns-or-sqs-or-eventbridge.html
