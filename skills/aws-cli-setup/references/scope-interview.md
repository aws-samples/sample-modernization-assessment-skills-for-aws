# Scope interview

The user should not have to know what to put in a prompt. "Start the assessment" is enough; the
agent looks around read-only, then asks five questions with real choices and defaults, and writes
`scope.json`. Every later stage reads that file and never re-asks.

## 1. Discover first, then ask

`scope_discovery.py` writes `01-setup/scope-options.json` in one or two minutes with:

| Field | What it holds | Used for |
|---|---|---|
| `identity` | account id, caller ARN | heading of the interview, account check |
| `regions` | per probed region: resource count (Resource Groups Tagging API) or the error | question 1 choices |
| `stacks` | CloudFormation stacks per active region with resource count and top types | question 2a choices |
| `tags` | most common tag keys and their values per active region, conventional keys first | question 2b choices |
| `ssm_managed_instances` | SSM-online instances per region | question 3 (only asked when > 0) |
| `denied_or_failed` | calls that were denied or failed | tell the user up front |
| `proposed_defaults` | regions with resources; the single stack if there is exactly one, else whole account | the `[default]` markers |

Calls are describe/list/get only. If `regions` are all errors, the identity lacks tagging-API
read access; fall back to asking the user for regions directly and say why.

## 2. The four questions

Ask only what the request did not already answer. Defaults in brackets.

1. **Regions** - "Resources were found in: us-east-1 (62), ap-northeast-2 (3). Assess which?"
   [all regions that hold resources]
2. **Workload filter** - one of:
   - a) a CloudFormation stack, listed with resource counts and top types
   - b) a tag, listed as `Key=Value (N resources)`
   - c) the whole account
   [the single stack if exactly one exists, otherwise whole account]. Offer `exclude` when
   obvious tooling stacks are present (CDKToolkit, vscode-server, workshop scaffolding).
3. **Host inspection** - only when SSM-online instances exist in the chosen scope: "N instances are
   reachable through SSM. Inspect running processes and services on them? This is the one write
   the assessment can make (SSM Run Command)." [no]
4. **Report language** - [English]. The report is in one language from start to finish. Only an explicit `lang <tag>` in the
   prompt (for example `lang ko`) or the reply `4 ko` changes it; the language the person writes in does not. Every
   artifact is written in English and, for another language, the finished report is translated once at the end.
5. **Context the report should not have to ask about** - three short facts the account team usually
   already knows, each with an "unknown" default so `confirm` still works:
   - environment: production (serves customers) / non-production [unknown]
   - who operates it day to day: internal team / outside operations provider (SI, MSP) / mixed [unknown]
   - AWS Support plan: basic / developer / business / enterprise-on-ramp / enterprise [unknown]
   - compliance the workload must show: names such as ISMS-P, PCI DSS, SOC 2, or none [unknown]

   Pass them as `--environment-type`, `--operator`, `--support-plan`, `--compliance "ISMS-P,PCI DSS"`. Later stages read them: the
   report drops the support-plan question when the plan is known, key issues stop hedging on "if
   this serves customer traffic" when the environment is known, and the owner vocabulary can name the
   operations provider. Asking the customer for facts the account team already has reads as not knowing the account.

Do not ask about: services (use the defaults; the collector's discovery scan enables listings for every
tagged service it finds, so name a service only when the owner says it exists but carries no tags), API budgets, output directory, or approval to proceed. Those are the
skill's job.

## 3. How to present it per client

The content is identical; only the surface differs.

- **A structured question tool is available** (Claude Code `AskUserQuestion`): submit the five
  questions in one call, each with its choices and the default pre-marked. Use the answers as given.
- **No structured question tool** (Kiro CLI, Codex): send one numbered message using the template
  below and wait. Accept a short reply: `confirm` (or `확정`) takes every default; otherwise read
  "number value" pairs and keep defaults for anything not mentioned. Never ask the questions one
  per turn.

```
Account XXXXXXXXXXXX verified (ModernizationAssessmentRole, read-only).
Found resources in us-east-1 (62) and ap-northeast-2 (3). GuardDuty queries were denied.

1. Regions                 [default: us-east-1, ap-northeast-2]
2. Workload                a) stack ecommerce-demo (14 resources: EC2, ALB, SG ...)
                           b) tag Environment=prod (12) · Project=shop (8)
                           c) whole account          [default: a]
3. Host inspection (SSM)   3 instances online        [default: no]
4. Report language         [default: English; lang ko for Korean]
5. Context                 environment prod/non-prod · operator internal/provider · support plan · compliance   [default: unknown]

Reply "confirm" to proceed with the defaults, or give the numbers you want to change
(for example: "1 us-east-1 only, 3 yes, 5 prod provider business").
```

Reading the reply:

| Reply | Meaning |
|---|---|
| `confirm`, `확정`, `ok`, `go` | all defaults |
| `1 us-east-1` | regions = us-east-1 only; the rest default |
| `2 c` or `2 account` | whole account |
| `2 b Environment=prod` | tag filter |
| `3 yes` / `3 i-0abc..., i-0def...` | host inspection on (all SSM-online instances in scope, or the listed ones) |
| `4 ko` / `lang ko` | report language Korean (default English) |
| `defaults` / `기본값으로` (anywhere, including the original prompt) | skip the interview; `confirmed_by: defaults` |

If the user's original prompt already named a region, stack, tag, workload name or language, do
not ask that question; record `confirmed_by: prompt` for the whole scope when nothing had to be
asked, `interview` otherwise. A workload name alone does not answer the workload filter or the
context questions (environment, operator, support plan, compliance): take those only from what
the person stated, and otherwise ask them or record `unknown`.

## 4. Write and validate

```bash
python3 "$SKILL_DIR/scripts/scope.py" write --account "$ACCOUNT" --regions us-east-1 \
  --workload-name ecommerce-demo --type stack --stack ecommerce-demo --exclude-stack vscode-server \
  --inspect-instance i-0123456789abcdef0 --language ko --profile assessment \
  --confirmed-by interview --output "$RUN_DIR/scope.json"
python3 "$SKILL_DIR/scripts/scope.py" validate "$RUN_DIR/scope.json"
```

`workload_name` becomes the artifacts folder name: use the stack name, the tag value, or the
account alias; letters, digits, `.`, `_`, `-` only. The writer refuses credential values in `auth`.

## 5. What later stages do with it

| Stage | Reads from scope.json |
|---|---|
| aws-mcp-setup | `output_language` → `localize_artifacts.py language` |
| aws-as-is-analysis | `regions`, `services`, `auth` → `collect.py` arguments; `workload` → which stack/tag to pull templates and sources for; `host_inspection` → whether to run `inspect_host.py --execute` and on which instances |
| aws-architecture-review | `workload` → the assessment basis (which baseline checks are out of scope) |
| aws-modernization-pathway | `workload_name`, `output_language` → report title and language |

A stage that finds no `scope.json` in the run directory asks the user to run aws-cli-setup first;
it does not invent a scope.
