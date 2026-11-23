# Run metadata: LLM model, inference time and usage (tokens or credits)

The review and recommendation reports show which LLM produced the assessment, the total
inference time and the usage in the unit the client meters: tokens for Claude Code and Codex, **credits for
Kiro CLI** (Kiro does not expose token counts; the report shows credits in their place and never converts
between the two). The Python helpers cannot observe any of this: they
run inside an agent session whose model, latency and token accounting belong to the client.
The agent therefore **declares** the values and the helpers only validate, total and display
them. Missing values stay `null` and render as "not recorded"; the helpers never estimate.

## File

`RUN_DIR/run-metadata.json`, schema 1.0:

```json
{"schema_version": "1.0", "agent_client": "kiro-cli", "model_id": "model-name",
 "notes": ["..."],
 "entries": [{"label": "stages 1-5", "model_id": null, "source": "kiro-cli /stats export",
              "recorded_at": "2026-10-20T10:30:00+00:00", "inference_seconds": 412.5,
              "input_tokens": 180000, "output_tokens": 22000, "requests": 37}]}
```

Entries are segments (per stage, per session or one total). Totals are summed over entries;
a field missing from some entries is flagged as partial. `model_id` on an entry overrides the
file-level model so model switches mid-run are visible.

## Recording

```bash
python3 "$SKILL_DIR/scripts/run_metadata.py" init --output "$RUN_DIR/run-metadata.json" --model MODEL --client CLIENT
python3 "$SKILL_DIR/scripts/run_metadata.py" import-claude-transcript --file "$RUN_DIR/run-metadata.json" \
    --transcript ~/.claude/projects/<workspace>/<session>.jsonl --label "stages 1-5" --since 2026-10-20T07:12:00Z
python3 "$SKILL_DIR/scripts/run_metadata.py" import-codex-rollout --file "$RUN_DIR/run-metadata.json" \
    --rollout ~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl --label "stages 1-5" --since ... --until ...
python3 "$SKILL_DIR/scripts/run_metadata.py" import-kiro-stats --file "$RUN_DIR/run-metadata.json" \
    --stats stats.json --label "session 1" [--since ...] [--until ...]
python3 "$SKILL_DIR/scripts/run_metadata.py" add --file "$RUN_DIR/run-metadata.json" --label "04-review" \
    --inference-seconds 412.5 --input-tokens 180000 --output-tokens 22000 --requests 37
python3 "$SKILL_DIR/scripts/run_metadata.py" show --file "$RUN_DIR/run-metadata.json"
```

Importers read only `type`, `timestamp`, model names and token counts; prompt and response text is
never copied into the run directory. Client logs are session-scoped, so bound them to the
assessment with `--since/--until` (ISO-8601 UTC, string comparison against the log timestamps).

Most input tokens are cache reads: the context the client re-sends with every request. The report's
provenance section shows the cache-read share and the number of sub-agent sessions included, because that is
where usage goes. Prices differ by client and provider and are not in any of these records, so the helpers
record tokens, requests and credits and never a cost.

| Client | Where usage lives | Importer | Tokens | Inference time |
|---|---|---|---|---|
| Claude Code | `~/.claude/projects/<workspace>/<session>.jsonl`; each `assistant` record carries `message.model` and `message.usage` (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`); streamed snapshots repeat the same `uuid`. Sub-agents the session starts write their own transcripts to `<session>/subagents/agent-*.jsonl` next to it | `import-claude-transcript` reads the session transcript and the `subagents/` transcripts, deduplicates by `uuid`, reports uncached + cache-creation + cache-read as `input_tokens` and keeps the parts, the sub-agent count and their share in `breakdown` | per request | not in the transcript; `/cost` shows Total duration (API) |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`; one `token_usage_record` per model response (`payload.response_id`, `payload.usage` with `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`; `input_tokens` includes the cached part); older rollouts only have `event_msg` records with `payload.type = token_count` holding a cumulative `total_token_usage`. `turn_context.payload.model` names the model. Sub-agents run as separate rollouts whose `session_meta` names the parent thread | `import-codex-rollout` sums the per-response records of the rollout and of every rollout that names it (or one of its sub-agents) as parent, in the same day folder and the next; without records it falls back to the delta of the cumulative snapshots. `--no-subagents` counts the one rollout only | per response | not recorded; `/status` shows token totals only |
| Kiro CLI V3 | `~/.kiro/sessions/<workspace hash>/sess_*/messages.jsonl` (plus `sub-executions/*.jsonl`): `usage_summary` records per turn with `elapsedTime` (ms), `requestIds` and credits (`promptTurnSummaries[].usage`); `assistant` records carry `reasoningModelId`. Token counts are not written. | `import-kiro-session --project-root DIR` finds the sessions whose workspace covers the project and sums them; credits go to `breakdown.credits` | not available (stays null) | summed turn `elapsedTime` (wall clock of the turn, model plus tool time) |
| Kiro CLI 2.x | `/stats save FILE` exports the last 100 requests with `duration_ms`, `input_tokens`, `output_tokens`, `timestamp` | `import-kiro-stats` sums them; export per stage because of the 100-request ring buffer | per request | summed `duration_ms` |
| Any client | provider usage report, session summary | `add` with explicit values | as reported | as reported |

Record the model from the client session (`/model` in Kiro and Claude Code, `/status` or
`-m` in Codex); importers also collect model names seen in the logs and flag a run that used
several models. `/usage`-style commands are account-level and not suitable for a run.
Transcript formats are client-owned and can change; the importers skip unknown lines and fail
when no usage record falls inside the window rather than guessing.

`inference_seconds` is model request time, not wall-clock time of the whole assessment, which
also includes CLI collection, rendering and human review.

## Reporting

`review.py --run-metadata` and `recommend.py --run-metadata` add a "Run provenance" section
(KPI tiles, totals table, per-segment table, notes) to the HTML, a matching Markdown section
and a `run_metadata` object in `findings.json` / `pathways.json`. Reports without the flag show
"model not recorded". Narrative labels are English drafts and localize with the
[output-language workflow](output-language.md); numbers, model IDs and timestamps are
protected values.
