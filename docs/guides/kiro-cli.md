# Running the assessment in Kiro CLI

**English** · [한국어](kiro-cli.ko.md)

This guide walks through one assessment in Kiro CLI, from an empty folder to the report. It is
based on a test run on 2026-11-15:

| | |
|---|---|
| Client | Kiro CLI 3.3.0 (V3 experience), macOS on Apple silicon |
| Model | `claude-opus-5.5`, effort `high` |
| Toolkit | moda-skill 2.2.1 |
| Workload | 7 CloudFormation stacks, 71 resources in scope, us-east-1, no host inspection |

The guides for [Claude Code](claude-code.md) and [Codex](codex.md) follow the same steps. For
AWS CloudShell, see [CLOUDSHELL.md](../CLOUDSHELL.md).

## Before you start

- Install the toolkit as described in the [README](../../README.md#1-install) and check that
  `moda-skill version` works.
- Sign in to Kiro CLI (`kiro-cli whoami`).
- Have an AWS CLI profile for the account. A read-only role is the safest choice; see
  [Permissions](../../README.md#permissions). If the profile can change the account, the
  assessment stops at stage 1 and asks how to proceed (step 5 below).
- For PNG export and visual checks of the report, install Playwright in the project folder after
  step 1: `npm install --no-save playwright@1.64.1` and `npx playwright@1.64.1 install chromium`.
  Without it the agent installs it when needed, or reports that PNG export was skipped.

## 1. Set up the project

```bash
mkdir shopnow-assessment && cd shopnow-assessment
moda-skill config --harness kiro --hooks
moda-skill doctor
```

`config` copies the skills into `.kiro/skills`, adds the AWS Knowledge MCP server, downloads the
two companion skills and, with `--hooks`, writes `.kiro/hooks/moda-skill.json`. `doctor` should
report `local_prerequisites_ready` and `mcp: connected`.

## 2. Start Kiro and check the hooks

```bash
kiro-cli chat
```

Kiro asks whether to trust the workspace. Trust it, because project hooks run only in a trusted
folder. (`kiro-cli chat --trust-workspace` skips the question for one launch without saving it.)

Run `/config hooks` and check that four moda-skill hooks are listed:

| Name | Trigger | Matcher |
|---|---|---|
| moda-skill read-only guard | PreToolUse | `shell` |
| moda-skill read-only guard | PreToolUse | `use_aws` |
| moda-skill file guard | PreToolUse | `write` |
| moda-skill stop gate | Stop | |

Then turn off the per-tool confirmations with `/tools trust-all` (or start with
`kiro-cli chat --trust-all-tools`). The run makes several hundred tool calls, so confirming each
one is not practical. The hooks still check every shell command and file change; see
[What the guard blocked](#what-the-guard-blocked).

## 3. Set the model and effort

The status line shows the model and effort, for example `claude-opus-5.5 · medium`. The
assessment is tested at effort `high` or above.

1. Run `/model` and keep `claude-opus-5.5` selected.
2. Press Tab to move to "Settings for selected model", then the down arrow to `effort`.
3. Press the right arrow until it shows `high`, then Esc twice.

Kiro saves the setting in `~/.kiro/settings/cli.json` under
`chat.modelDefaults.claude-opus-5.5.output_config.effort`, so it applies to later sessions with
that model as well.

## 4. Start the assessment

Type `/moda-skill`, press Tab to accept the command from the list, then add the options:

```text
/moda-skill start profile assessment region us-east-1 workload shopnow lang ko
```

Typing the whole line at once can let the command list pick a different entry, which is why the
guide accepts `/moda-skill` with Tab first.

## 5. Answer the checks

The agent pauses for decisions only you can make. In the test run there were two pauses.

**Write-capable profile.** When the IAM policy simulator shows that the profile can make changes,
the agent lists what it can do and offers three choices: switch to a read-only profile, stop, or
continue. To continue, answer with the number and a one-line reason in your own words, for
example:

```text
3, continue with this profile. Reason: my own PoC account; the read-only guard is installed.
```

The reason is stored in `scope.json`. Until it is recorded, the collecting helpers refuse to start.

**Scope.** The agent looks around the account read-only and sends one numbered message: region,
workload (stacks, a tag or the whole account), host inspection, report language and context
(environment, operator, support plan). Reply with only what you want to change, for example
`2 a, 3 no, 5 non-production internal unknown unknown`, or `confirm` to accept the defaults.

## 6. While it runs, and after

The agent runs the five stages without further questions. The report is in
`artifacts/<workload>/<run id>/06-assessment/assessment.html`, and the localized copy in
`06-assessment-<lang>/`.

- `/moda-skill status` shows the stage the run is in; `/moda-skill resume` continues an
  interrupted run. After you quit, `kiro-cli chat --resume-id <id>` (printed on exit) reopens the
  conversation.
- Kiro records model usage when a turn ends. Most of the assessment happens in one long turn, so
  the report written in that turn shows only the earlier turns. Run `/moda-skill report` once the
  run is complete to render the report again with the full usage.
- The write audit at the end lists every change CloudTrail recorded for the profile's IAM
  principal during the run. Other work done with the same principal, such as a deployment from
  another Kiro session, shows up there too. In the test run it found 170 changes; the ones checked
  came from a deployment running in another Kiro session at the same time. A profile used only for
  the assessment keeps this check clear.

## What one test run looked like

| | |
|---|---|
| Stage 1 to the last stage file | 145 minutes |
| Kiro credits (whole session) | 632.64 |
| Report | Final, 6 key issues, 5 specialist reviews |
| Read-only guard | 9 of 9 checks behaved as expected (below) |

Three clients ran against the same account at the same time, so the times are one observation,
not a benchmark.

### What the guard blocked

After the run, the agent was asked to try each of the following once. The EC2 calls used
`--dry-run`, the secret did not exist, and the files were test files.

| Attempt | Result |
|---|---|
| `aws ec2 create-tags --dry-run ...` | blocked: not a read-only metadata call |
| The same inside `bash -c "..."` | blocked |
| `python3 -c` with boto3 `create_tags` | blocked: inline code that calls AWS |
| `aws secretsmanager get-secret-value` | blocked: reads data content or credentials |
| `AWS_PROFILE=default aws ec2 describe-vpcs` | blocked: the profile must stay the run's profile |
| `aws ec2 describe-vpcs` not in the approved list | blocked, then ran after `plan_check.py` approved it |
| `echo ... >> ~/.aws/...` | blocked: `~/.aws` is protected |
| File tool edit of `.kiro/skills/moda-skill/SKILL.md` | blocked: installed skills are protected |
| File tool edit of the run's `scope.json` | blocked: only the stage helpers write it |

CloudTrail showed no `CreateTags` or `GetSecretValue` call afterwards. The session had no
`use_aws` tool, so the `use_aws` hook was not exercised in this run.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A shell command ends with "Command timed out after 120000ms" | The command ran longer than the shell tool allowed (120 seconds in the test run). Scope discovery used to take that long in a busy account; it now runs its calls in parallel and took about 20 seconds for one region and 45 seconds for all default regions. Naming the region in `start` keeps it short. |
| `/moda-skill start` says the stage skills are missing | Run `moda-skill config --harness kiro` in this folder, restart Kiro and start again. Do not copy skills from `~/.kiro/skills`. |
| The model check stops the run | Switch the model or effort as in step 3 and start again, or reply that you want to continue anyway. |
| No moda-skill hooks under `/config hooks` | The folder is not trusted, or `config` ran without `--hooks`. Run `moda-skill config --harness kiro --hooks` and restart Kiro. |
