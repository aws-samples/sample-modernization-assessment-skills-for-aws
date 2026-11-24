#!/usr/bin/env python3
"""Record agent-declared LLM usage (model, inference time, tokens) for an assessment run.

The assessment helpers cannot observe which model drove the agent, how long inference took or how
many tokens were used. The agent records those values here from its client telemetry (for example
Kiro CLI `/stats save FILE` exports, or a provider usage report) and the review/recommendation
reports display them. Missing values stay null and are shown as "not recorded"; nothing is estimated.

  python3 run_metadata.py init --output RUN_DIR/run-metadata.json --model MODEL --client kiro
  python3 run_metadata.py add  --file RUN_DIR/run-metadata.json --label "04-review" \\
          --inference-seconds 412.5 --input-tokens 180000 --output-tokens 22000 --requests 37
  python3 run_metadata.py import-kiro-stats --file RUN_DIR/run-metadata.json --label "stages 1-5" \\
          --stats stats.json [--since 2026-10-20T07:12:00Z] [--until ...]
  python3 run_metadata.py import-claude-transcript --file RUN_DIR/run-metadata.json --label "session" \\
          --transcript ~/.claude/projects/<workspace>/<session>.jsonl [--since ...] [--until ...]
  python3 run_metadata.py import-codex-rollout --file RUN_DIR/run-metadata.json --label "session" \\
          --rollout ~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl [--since ...] [--until ...]
  python3 run_metadata.py show --file RUN_DIR/run-metadata.json

Importers read only model names, timestamps and token counts from the client logs; prompt and
response text is never copied. Client logs are session-scoped, so pass --since/--until (ISO-8601
UTC) to bound them to the assessment run. None of the three clients logs per-request inference
time in these files; record it with `add --inference-seconds` from the client's own summary
(Claude Code `/cost` "Total duration (API)", Codex `/status`, Kiro `/stats`) or leave it null.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "aws-architecture-review" / "scripts"))
try:
    import report_kit  # noqa: E402
except ImportError:  # standalone copy without the sibling skill
    report_kit = None

SCHEMA = "1.0"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA:
        raise ValueError("unsupported run metadata schema")
    return data


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def number(value, name, integer=False):
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return int(value) if integer else float(value)


def cmd_init(args):
    path = Path(args.output)
    if path.exists() and not args.force:
        raise ValueError(f"{path} exists; use add to append or --force to replace")
    data = {"schema_version": SCHEMA, "created_at": now(), "agent_client": args.client, "model_id": args.model,
            "notes": ["Values are declared by the agent from client telemetry; the assessment helpers do not measure model usage."]
            + ([args.note] if args.note else []), "entries": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, data)
    return {"status": "created", "file": str(path)}


def cmd_add(args):
    data = load(args.file)
    entry = {"label": args.label, "model_id": args.model, "source": args.source, "recorded_at": now(),
             "inference_seconds": number(args.inference_seconds, "inference-seconds"),
             "input_tokens": number(args.input_tokens, "input-tokens", True),
             "output_tokens": number(args.output_tokens, "output-tokens", True),
             "requests": number(args.requests, "requests", True)}
    if all(entry[k] is None for k in ("inference_seconds", "input_tokens", "output_tokens", "requests")) and not args.allow_empty:
        raise ValueError("entry has no values; pass --allow-empty to record an explicitly unknown segment")
    data["entries"].append(entry)
    save(args.file, data)
    return {"status": "added", "entries": len(data["entries"])}


def cmd_import_kiro_stats(args):
    """Aggregate a Kiro CLI `/stats save` export: duration_ms, input_tokens, output_tokens per request."""
    records = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("stats export must be a JSON array")
    kept = [r for r in records if isinstance(r, dict) and in_window(r.get("timestamp"), args.since, args.until)]
    if not kept:
        raise ValueError("no stats records in range")
    seconds = sum(float(r.get("duration_ms") or 0) for r in kept) / 1000
    inp = sum(int(r.get("input_tokens") or 0) for r in kept)
    out = sum(int(r.get("output_tokens") or 0) for r in kept)
    data = load(args.file)
    data["entries"].append({"label": args.label, "model_id": args.model, "source": f"kiro-cli /stats export ({Path(args.stats).name})",
                            "recorded_at": now(), "inference_seconds": round(seconds, 1), "input_tokens": inp, "output_tokens": out,
                            "requests": len(kept)})
    data.setdefault("notes", []).append("Kiro /stats keeps only the last 100 requests of a session; long sessions may be under-counted. Export per stage.")
    save(args.file, data)
    return {"status": "imported", "requests": len(kept), "inference_seconds": round(seconds, 1), "input_tokens": inp, "output_tokens": out}


def kiro_session_dirs(project_root, since=None):
    """Kiro CLI V3 session folders whose workspace includes project_root (~/.kiro/sessions/<hash>/sess_*/).

    session.json carries workspacePaths and timestamps; messages.jsonl carries usage_summary records with elapsedTime
    (ms), requestIds and credits per turn, plus assistant records with reasoningModelId. Token counts are not written
    by Kiro CLI V3, so they stay null here; credits and request counts are what the client exposes."""
    root = Path.home() / ".kiro/sessions"
    project = str(Path(project_root).resolve())
    found = []
    for meta in root.glob("*/sess_*/session.json") if root.is_dir() else []:
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        paths = [str(Path(p).resolve()) for p in (info.get("workspacePaths") or info.get("rootPaths") or []) if isinstance(p, str)]
        if not any(project == p or project.startswith(p.rstrip("/") + "/") for p in paths):
            continue
        if since and str(info.get("lastModifiedAt", "")) < since:
            continue
        found.append((info.get("lastModifiedAt", ""), meta.parent, info))
    return [(d, i) for _, d, i in sorted(found)]


def summarize_kiro_session(folder, since=None, until=None):
    seconds = 0.0; requests = 0; credits = 0.0; turns = 0; models = set()
    files = [folder / "messages.jsonl"] + sorted((folder / "sub-executions").glob("*.jsonl"))
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            if not in_window(record.get("timestamp"), since, until):
                continue
            if payload.get("type") == "usage_summary":
                turns += 1
                seconds += float(payload.get("elapsedTime") or 0) / 1000
                requests += len(payload.get("requestIds") or [])
                credits += sum(float(x.get("usage") or 0) for x in payload.get("promptTurnSummaries") or [])
            elif payload.get("type") == "assistant" and payload.get("reasoningModelId"):
                models.add(str(payload["reasoningModelId"]).split("::")[-1])
    return {"turns": turns, "elapsed_seconds": round(seconds, 1), "requests": requests, "credits": round(credits, 2), "models": sorted(models)}


def cmd_import_kiro_session(args):
    """Read Kiro CLI V3 session records for this project; no /stats export needed."""
    sessions = kiro_session_dirs(args.project_root, args.since)
    if not sessions:
        raise ValueError(f"no Kiro CLI session under ~/.kiro/sessions covers {args.project_root}")
    data = load(args.file)
    added = []
    for folder, info in sessions[-args.last:]:
        summary = summarize_kiro_session(folder, args.since, args.until)
        if not summary["turns"]:
            continue
        entry = {"label": f"{args.label} ({info.get('title') or folder.name})"[:120], "model_id": summary["models"][0] if len(summary["models"]) == 1 else None,
                 "source": f"kiro-cli session {folder.name}", "recorded_at": now(), "inference_seconds": summary["elapsed_seconds"],
                 "input_tokens": None, "output_tokens": None, "requests": summary["requests"],
                 "breakdown": {"credits": summary["credits"], "turns": summary["turns"], "models": summary["models"],
                               "note": "elapsed time is turn wall-clock as reported by Kiro (model plus tool time); Kiro CLI V3 does not record token counts"}}
        # Kiro writes a turn's usage only when the turn ends, so an import made while the long assessment turn was still
        # running under-counts. A later import of the same session replaces that entry instead of adding to it.
        data["entries"] = [e for e in data["entries"] if e.get("source") != entry["source"]]
        data["entries"].append(entry)
        added.append({"session": folder.name, **summary})
    if not added:
        raise ValueError("matching sessions have no usage_summary records")
    data.setdefault("notes", [])
    note = "Kiro CLI V3 session records expose credits, requests and turn elapsed time, not tokens; tokens stay 'not recorded'."
    if note not in data["notes"]:
        data["notes"].append(note)
    save(args.file, data)
    return {"status": "imported", "sessions": added}


def in_window(stamp, since, until):
    stamp = str(stamp or "")
    return (not since or stamp >= since) and (not until or stamp <= until)


def read_jsonl(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue  # client logs can contain partial trailing lines


def claude_usage(path, since, until, seen):
    """Token totals, request count and models from one Claude Code transcript, deduplicated across files by message id."""
    totals = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    requests, models = 0, set()
    for record in read_jsonl(path):
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage")
        key = record.get("uuid") or message.get("id")
        if not isinstance(usage, dict) or not key or key in seen or not in_window(record.get("timestamp"), since, until):
            continue
        seen.add(key)
        requests += 1
        if message.get("model"):
            models.add(str(message["model"]))
        for field in totals:
            value = usage.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                totals[field] += int(value)
    return totals, requests, models


def claude_subagent_transcripts(transcript):
    """Transcripts of the sub-agents a Claude Code session started: <session>/subagents/agent-*.jsonl next to it."""
    folder = Path(transcript).with_suffix("") / "subagents"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def cmd_import_claude_transcript(args):
    """Sum per-assistant-message `message.usage` from a Claude Code session transcript (JSONL) and from the transcripts
    of the sub-agents that session started (the `subagents/` folder beside it).

    Deduplicates by message uuid (streamed snapshots repeat), counts uncached input, cache creation and
    cache reads separately and reports their sum as input_tokens. Inference time is not in the transcript.
    """
    seen = set()
    totals, requests, models = claude_usage(args.transcript, args.since, args.until, seen)
    sub_files = claude_subagent_transcripts(args.transcript)
    sub_requests, sub_totals = 0, {k: 0 for k in totals}
    for path in sub_files:
        t, r, m = claude_usage(path, args.since, args.until, seen)
        sub_requests += r; models |= m
        for k in totals:
            totals[k] += t[k]; sub_totals[k] += t[k]
    requests += sub_requests
    if not requests:
        raise ValueError("no assistant usage records in range")
    data = load(args.file)
    model = args.model or (next(iter(models)) if len(models) == 1 else None)
    source = f"claude-code transcript ({Path(args.transcript).name})"
    data["entries"] = [e for e in data["entries"] if e.get("source") != source]  # a re-import of the same session replaces, not adds
    data["entries"].append({"label": args.label, "model_id": model, "source": source,
                            "recorded_at": now(), "inference_seconds": None,
                            "input_tokens": totals["input_tokens"] + totals["cache_creation_input_tokens"] + totals["cache_read_input_tokens"],
                            "output_tokens": totals["output_tokens"], "requests": requests,
                            "breakdown": {"uncached_input": totals["input_tokens"], "cache_creation_input": totals["cache_creation_input_tokens"],
                                          "cache_read_input": totals["cache_read_input_tokens"], "models": sorted(models),
                                          "subagents": len(sub_files), "subagent_requests": sub_requests,
                                          "subagent_input_tokens": sum(sub_totals[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                                          "subagent_output_tokens": sub_totals["output_tokens"]}})
    data.setdefault("notes", []).append("Claude Code transcripts carry token usage but no per-request latency; record inference time from /cost (Total duration (API)) with add --inference-seconds if needed.")
    save(args.file, data)
    return {"status": "imported", "requests": requests, "subagents": len(sub_files), "subagent_requests": sub_requests, "models": sorted(models), **totals}


CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")


def codex_session_meta(path):
    """(thread id, parent thread id) from a rollout's session_meta record."""
    for record in read_jsonl(path):
        if isinstance(record, dict) and record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            source = payload.get("source")
            parent = payload.get("parent_thread_id")
            if not parent and isinstance(source, dict):
                parent = ((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id")
            return str(payload.get("id") or payload.get("session_id") or ""), (str(parent) if parent else None)
    return "", None


def codex_subagent_rollouts(rollout):
    """Rollouts of the sub-agents a Codex session started, found by parent thread id in the same day folder and the
    next day's folder (a run can cross midnight). Nested sub-agents are followed too."""
    rollout = Path(rollout).resolve()
    root_id, _ = codex_session_meta(rollout)
    if not root_id:
        return []
    folders = [rollout.parent]
    try:
        day = datetime.strptime("/".join(rollout.parts[-4:-1]), "%Y/%m/%d")
        folders.append(rollout.parents[3] / (day + timedelta(days=1)).strftime("%Y/%m/%d"))
    except ValueError:
        pass
    candidates = [p for folder in folders if folder.is_dir() for p in sorted(folder.glob("rollout-*.jsonl")) if p.resolve() != rollout]
    metas = {p: codex_session_meta(p) for p in candidates}
    wanted, found = {root_id}, []
    changed = True
    while changed:
        changed = False
        for p, (tid, parent) in metas.items():
            if parent in wanted and tid not in wanted:
                wanted.add(tid); found.append(p); changed = True
    return found


def codex_usage(path, since, until, seen):
    """Per-request token usage from one Codex rollout. Prefers `token_usage_record` rows (one per model response, with a
    response id); falls back to the delta of cumulative `token_count` snapshots for rollouts that lack them."""
    totals = {k: 0 for k in CODEX_FIELDS}
    requests, models = 0, set()
    records_seen = False
    before, last, previous = None, None, None
    snapshot_requests = 0
    for record in read_jsonl(path):
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") or {}
        stamp = record.get("timestamp")
        if record.get("type") == "turn_context" and payload.get("model") and in_window(stamp, since, until):
            models.add(str(payload["model"]))
        if record.get("type") == "token_usage_record" or payload.get("type") == "token_usage_record":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            key = payload.get("response_id") or f"{path.name}:{stamp}"
            if usage is None or key in seen or not in_window(stamp, since, until):
                continue
            records_seen = True
            seen.add(key)
            requests += 1
            for field in CODEX_FIELDS:
                value = usage.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    totals[field] += int(value)
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        total = (payload.get("info") or {}).get("total_token_usage")
        if not isinstance(total, dict):
            continue
        if since and str(stamp or "") < since:
            before = total
            continue
        if until and str(stamp or "") > until:
            continue
        last = total
        snap = (total.get("input_tokens"), total.get("output_tokens"))
        if snap != previous:
            snapshot_requests += 1
            previous = snap
    if records_seen:
        return totals, requests, models, "records"
    if last is None:
        return totals, 0, models, "none"
    for field in CODEX_FIELDS:
        end = last.get(field) or 0
        start = (before or {}).get(field) or 0
        if end < start:
            raise ValueError(f"cumulative {field} decreased inside the window; the rollout may span a session reset")
        totals[field] = int(end - start)
    return totals, snapshot_requests, models, "snapshots"


def cmd_import_codex_rollout(args):
    """Sum the token usage of a Codex session and of the sub-agent sessions it started.

    Each rollout's `token_usage_record` rows (one per model response) are summed; rollouts without them fall back to the
    delta of the cumulative `token_count` snapshots. Sub-agent rollouts are found by their parent thread id."""
    seen = set()
    main_path = Path(args.rollout)
    totals, requests, models, method = codex_usage(main_path, args.since, args.until, seen)
    sub_files = [] if args.no_subagents else codex_subagent_rollouts(main_path)
    sub_requests, sub_totals = 0, {k: 0 for k in CODEX_FIELDS}
    for path in sub_files:
        t, r, m, _ = codex_usage(path, args.since, args.until, seen)
        sub_requests += r; models |= m
        for k in CODEX_FIELDS:
            totals[k] += t[k]; sub_totals[k] += t[k]
    requests += sub_requests
    if not requests:
        raise ValueError("no token usage records in range")
    data = load(args.file)
    model = args.model or (next(iter(models)) if len(models) == 1 else None)
    source = f"codex rollout ({main_path.name})"
    data["entries"] = [e for e in data["entries"] if e.get("source") != source]  # a re-import of the same session replaces, not adds
    data["entries"].append({"label": args.label, "model_id": model, "source": source,
                            "recorded_at": now(), "inference_seconds": None,
                            "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"], "requests": requests,
                            "breakdown": {"cached_input": totals["cached_input_tokens"], "cache_write_input": totals["cache_write_input_tokens"],
                                          "uncached_input": max(0, totals["input_tokens"] - totals["cached_input_tokens"]),
                                          "reasoning_output": totals["reasoning_output_tokens"], "models": sorted(models), "method": method,
                                          "subagents": len(sub_files), "subagent_requests": sub_requests,
                                          "subagent_input_tokens": sub_totals["input_tokens"], "subagent_output_tokens": sub_totals["output_tokens"]}})
    data.setdefault("notes", []).append("Codex rollouts carry per-response token usage but no latency; record inference time from /status with add --inference-seconds if needed.")
    save(args.file, data)
    return {"status": "imported", "requests": requests, "subagents": len(sub_files), "subagent_requests": sub_requests, "method": method,
            "models": sorted(models), "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"]}


def cmd_show(args):
    if not report_kit:
        raise ValueError("report_kit.py not found beside aws-architecture-review; show requires the installed skill set")
    meta = report_kit.load_run_metadata(args.file)
    return {"model": meta["models_seen"] or meta["model_id"], "totals": meta["totals"], "entries": len(meta["entries"]),
            "partial_fields": meta["partial_fields"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("--output", required=True); p.add_argument("--model"); p.add_argument("--client")
    p.add_argument("--note"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("add"); p.add_argument("--file", required=True); p.add_argument("--label", required=True); p.add_argument("--model")
    p.add_argument("--inference-seconds", type=float); p.add_argument("--input-tokens", type=int); p.add_argument("--output-tokens", type=int)
    p.add_argument("--requests", type=int); p.add_argument("--source", default="declared"); p.add_argument("--allow-empty", action="store_true")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("import-kiro-stats"); p.add_argument("--file", required=True); p.add_argument("--stats", required=True)
    p.add_argument("--label", required=True); p.add_argument("--model"); p.add_argument("--since"); p.add_argument("--until"); p.set_defaults(func=cmd_import_kiro_stats)
    p = sub.add_parser("import-claude-transcript"); p.add_argument("--file", required=True); p.add_argument("--transcript", required=True)
    p.add_argument("--label", required=True); p.add_argument("--model"); p.add_argument("--since"); p.add_argument("--until"); p.set_defaults(func=cmd_import_claude_transcript)
    p = sub.add_parser("import-codex-rollout"); p.add_argument("--file", required=True); p.add_argument("--rollout", required=True)
    p.add_argument("--label", required=True); p.add_argument("--model"); p.add_argument("--since"); p.add_argument("--until")
    p.add_argument("--no-subagents", action="store_true", help="count only this rollout, not the sub-agent sessions it started"); p.set_defaults(func=cmd_import_codex_rollout)
    p = sub.add_parser("import-kiro-session", help="Kiro CLI V3: read ~/.kiro/sessions for this project (credits, requests, elapsed time, model)")
    p.add_argument("--file", required=True); p.add_argument("--project-root", default=str(Path.cwd())); p.add_argument("--label", default="assessment session")
    p.add_argument("--since"); p.add_argument("--until"); p.add_argument("--last", type=int, default=1, help="how many most recent matching sessions to import")
    p.set_defaults(func=cmd_import_kiro_session)
    p = sub.add_parser("show"); p.add_argument("--file", required=True); p.set_defaults(func=cmd_show)
    args = parser.parse_args()
    try:
        print(json.dumps(args.func(args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Run metadata stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
