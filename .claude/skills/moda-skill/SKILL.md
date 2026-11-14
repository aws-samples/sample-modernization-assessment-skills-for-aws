---
name: moda-skill
description: "Entry point for the AWS Modernization Assessment, invoked as /moda-skill. Subcommands: prepare, start, status, resume, report, doctor, help, version. Also answers `prepare moda-skill` / `moda-skill 준비해줘` and `start the assessment` / `평가 시작해`. Routes to the stage skills and keeps the run on track."
user-invocable: true
argument-hint: "[prepare | start [opts] | status | resume | report | doctor | help | version]"
---

# moda-skill

You are the front door of the AWS modernization assessment. The person typed `/moda-skill` followed by an optional subcommand and arguments: **$ARGUMENTS**

Everything below is in English; speak to the person in the language they use. The assessment itself is read-only against AWS: describe/list/get calls only, no secrets, no data content. The stage skills do the work; you decide which one runs next and report plainly.

## Routing

Take the first word of the arguments as the subcommand. No arguments means `status`.

| Subcommand | What you do |
|---|---|
| `help` | Print the table under "Help" below, nothing else. |
| `version` | Run `python3 ".claude/skills/moda-skill/scripts/moda.py" version` and print its output. |
| `doctor` | Run `python3 ".claude/skills/moda-skill/scripts/moda.py" model-check --client claude` and include its one-line result. Then run `python3 ".claude/skills/aws-mcp-setup/scripts/doctor.py" --targets claude --scope project --project-root "$PWD" --probe-mcp --output "$PWD/.moda/doctor-<UTC timestamp>.json"`. Summarize `status`, `mcp` and `next_actions` in three lines. Also run `python3 ".claude/skills/moda-skill/scripts/moda.py" preflight --client claude`. If something is missing, say the fix is `moda-skill config --harness claude` in a terminal (or `/moda-skill prepare`). |
| `prepare` | Follow the **aws-skills-install** skill ("prepare moda-skill"): diagnose, fix, verify, readiness table. Scope `project`, client `claude` unless the arguments say otherwise. End with: restart the client, then `/moda-skill start`. |
| `start [options]` | Run `python3 ".claude/skills/moda-skill/scripts/moda.py" preflight --client claude` first; if it exits non-zero, show its message verbatim (the fix is the terminal command `moda-skill config --harness claude`, not an alternative copy of the skills elsewhere) and stop. Then run `python3 ".claude/skills/moda-skill/scripts/moda.py" model-check --client claude` (pass `--model <id> --effort <level>` when you know your own model; on Kiro the helper reads the session). If it exits non-zero, show its message to the person verbatim and stop: they switch the model or effort and run `start` again, or reply that they want to continue anyway. Then run `python3 ".claude/skills/moda-skill/scripts/moda.py" status`. If a run is already in progress, say so and ask whether to `resume` or start a new one; do not start twice. Otherwise follow the stage skills in order: **aws-cli-setup** (verify identity, scope interview, `scope.json`), **aws-mcp-setup**, **aws-as-is-analysis**, **aws-architecture-review**, **aws-modernization-pathway**, ending with the unified report in `06-assessment`. Before rendering that report, import the client's usage records into `run-metadata.json` as described under `report`, so the provenance section shows the model, elapsed time, requests and credits of this run instead of "not recorded". Pass the rest of the arguments to the scope interview verbatim: anything already named (profile, account, region, workload, stack, tag, language, host inspection) is not asked again. |
| `status` | Run `python3 ".claude/skills/moda-skill/scripts/moda.py" status` and print its table. If there is no run yet, say that `/moda-skill start` begins one. |
| `resume [run-dir]` | Run `python3 ".claude/skills/moda-skill/scripts/moda.py" preflight --client claude`; if it exits non-zero, show its message verbatim and stop. Then run `python3 ".claude/skills/moda-skill/scripts/moda.py" status --json` to find the latest run and its first incomplete stage, then continue from that stage skill with the files already on disk. Never redo a completed stage unless asked. |
| `report [run-dir]` | First refresh run metadata from the client's own records: on Kiro CLI run `python3 ".claude/skills/aws-mcp-setup/scripts/run_metadata.py" import-kiro-session --file "$RUN_DIR/run-metadata.json" --project-root "$PWD" --label "assessment"` (Claude Code: `import-claude-transcript`; Codex: `import-codex-rollout`; see the aws-mcp-setup run-metadata reference). Then re-render the unified report for the latest (or named) run: the `assessment_report.py` command from **aws-modernization-pathway** with the existing `key-issues.json`, diagrams and run metadata. If `key-issues.json` is missing, write it first from the findings. |
| anything else | Treat the whole text as a request. Natural-language triggers still work: "prepare…" is `prepare`; "start…", "assess…", "평가…" is `start` with the text as options. If it matches nothing, print Help. |

The run directory is `artifacts/<workload>/<UTC run id>/` under the current directory. `moda.py status` reads the stage artifacts (`01-setup/access-check.json`, `scope.json`, `02-toolkit/toolkit-check.json`, `03-as-is/inventory.json`, `03-as-is-enriched/`, `04-review/findings.json`, `05-modernization/pathways.json`, `06-assessment/assessment.html`) to decide what is done; you do not keep state anywhere else.

## Rules that apply to every subcommand

- Stage skills live in `.claude/skills/<name>/SKILL.md`. Read the one you are about to run; do not paraphrase it from memory.
- On Windows (PowerShell), run the helpers with `py -3` (or `python`) in place of `python3`, and `$PWD.Path` in place of `$PWD`.
- Before any AWS call, the identity check of aws-cli-setup must have passed for this run. Stop if the account differs from the one the person named.
- Say what you are doing in one line per stage. No play-by-play of tool calls.
- When a stage helper exits non-zero, show its last error line, name the cause, apply the fix the stage skill gives, and retry once. Then report and stop.
- Never write outside the project directory except where the stage skill names a path.
- When you must stop for a decision only the person can make (a profile name, scope confirmation, an ambiguous account), run `python3 ".claude/skills/moda-skill/scripts/hook.py" pause "<what you need>"` before asking; `start` and `resume` run `hook.py resume`. With hooks installed (`moda-skill config --hooks`), the stop gate otherwise asks you to continue an unfinished run, and the read-only guard blocks any `aws` call that is not describe/list/get. A blocked call is not an error to work around: use the metadata call, or report what you needed.
- Diagram redraws (stages 4 and 5) and the five Specialist SA Agent reviews of stage 5 go to sub-agents while you write checks, key issues and the synthesis; verify their output yourself (the diagram audit, `advice.py validate`). Without a sub-agent tool, do the same work in sequence.

## Help

```
/moda-skill                 show the current run and the next step
/moda-skill prepare         install or verify the stage skills, MCP server and companions
/moda-skill start [opts]    verify account, confirm scope, run all five stages, write the report
                               opts: profile <name> region <r> account <12 digits> workload <name>
                                     stack <name> | tag Key=Value | account-wide
                                     ssm <instance-id,...> | no-ssm   lang ko|en|...   use defaults
/moda-skill status          stage-by-stage progress of the latest run
/moda-skill resume          continue the latest run from its first incomplete stage
/moda-skill report          re-render 06-assessment for the latest run
/moda-skill doctor          check prerequisites, skills and the AWS Knowledge MCP connection
/moda-skill version         installed version
```

Examples: `/moda-skill start profile assessment region us-east-1 workload shopnow tag Project=shopnow lang ko` · `/moda-skill start use defaults`

Model: the assessment is designed and tested on the newest frontier model (currently `claude-opus-5.5`) at effort `high` or higher. Sonnet-class models produce visibly shallower key issues and recommendations; Haiku-class models skip steps. `model-check` says which tier the session is in.
