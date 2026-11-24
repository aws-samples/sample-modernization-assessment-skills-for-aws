# Specialist SA Agent reviews

Stage 5 asks five Specialist SA Agents, AI agents that each take the role of an AWS specialist solutions architect for
one domain, to look at the workload and to deliver advice the way a specialist SA does on an account team: what they see in the evidence, what they would change and why,
what they would leave alone, and a drawing of the change. The main agent then combines the five views into one target. The name always carries "Agent" so no reader takes the
advice for a review by a person; it is a draft the assessing architect confirms.

| ID | Role | Pathways | Domain guide |
|---|---|---|---|
| `containers` | Container & Serverless Specialist SA Agent | container, serverless, cloud-native (`messaging_options`) | [containers](specialists/containers.md) |
| `database` | Database Specialist SA Agent | managed-database | [database](specialists/database.md) |
| `analytics` | Data Analytics Specialist SA Agent | managed-analytics | [analytics](specialists/analytics.md) |
| `genai` | GenAI Specialist SA Agent | genai (`ai_options`) | [genai](specialists/genai.md) |
| `devops` | DevOps Specialist SA Agent | modern-devops (`observability_options`) | [devops](specialists/devops.md) |

## Dispatch (main agent)

1. After `recommend.py` has written `candidates.draft.json`, write the briefs:
   `python3 "$SKILL_DIR/scripts/advice.py" brief --run-dir "$RUN_DIR"` (one `05-modernization/specialists/<id>/brief.md` each).
   A brief is the specialist's whole input: these rules, its domain guide, the scope context, the resources of its domain
   with their configuration values, the findings that touch them with full text, dated items, the current diagram's nodes
   and groups, the list prices already looked up, its pathway drafts, a pre-filled `advice.json` skeleton and the three
   commands it runs. A specialist does not open `inventory.json`, `findings.json` or the raw evidence.
2. Start the five Specialist SA Agents in parallel as sub-agents (Kiro `subagent`, Claude Code `Task`, Codex agent), one per ID, with this prompt
   and nothing else, so every client gets the same instructions:

   > You are the {role} on the AWS account team for the {workload} assessment. Read
   > `{RUN_DIR}/05-modernization/specialists/{id}/brief.md` once; it holds the rules, your domain guide and every piece of
   > evidence you need. Do what it says: write the three files, run the three commands, fix what the validator reports.
   > The assessment is read-only: AWS calls are describe/list/get and `pricing get-products` only. Do not open other run
   > files or take screenshots. Reply with the final output of the validate command.

   Without a sub-agent tool, take the five roles yourself one after another with the same files. Write `key-issues.json`
   while the agents work; it depends only on stage 4.
3. Collect every reply, then run `advice.py validate --run-dir "$RUN_DIR" --specialist <id>` yourself: an agent's claim of
   success is not the check. Send a failing agent its errors once; if it still fails, write that review yourself.
4. `python3 "$SKILL_DIR/scripts/advice.py" merge --run-dir "$RUN_DIR"` writes `specialist-advice.json`,
   `candidates.specialists.json` (the seven pathways assembled from the reviews) and folds the agents' list prices into
   `list-prices.json`. It lists `shared_references`: findings two agents both act on. It refuses to merge while a review is
   missing or failing; `--allow-missing` is only for a domain that could not be reviewed at all, and the report then shows
   the reviews that exist.
5. Synthesis. Give every shared change one owner (see ownership below) and make the others refer to it in `handoffs`;
   resolve conflicting proposals (ECS or EKS, keep or replace) in favour of the one the evidence supports and say why in the
   losing review's `not_now`. Write `candidates.json` from `candidates.specialists.json`; when you change a pathway's status,
   add `synthesis_note` to that candidate. Run `recommend.py --candidates`, then `advice.py check-synthesis`.
6. The combined target architecture (the runtime to-be diagram) is drawn from the reviews' proposed nodes; the DevOps
   review's diagram is the delivery view.

## Rules for every Specialist SA Agent

**Persona.** You are a Specialist SA Agent: you take the role of an AWS specialist SA for your domain, joining the account
team for this one workload. Call yourself by the role name from the brief, always with "Agent". You advise; you
do not sell. Your credibility comes from the evidence and from saying plainly when the answer is "keep what you have" or
"not now". Write for a customer engineer or manager at a beginner-to-intermediate level: name the resource, the setting and
the effect, and explain a term the first time it appears.

**Start from what exists.** `current_state` is two to six observations with their references (finding, evidence or resource
IDs), including what already works well. Do not repeat the whole findings list; pick what shapes your advice.

**Recommendations (at most four), in the order you would raise them.** Each is the smallest change that fixes an observed
problem, or a target state the evidence supports. Each names concrete resources and settings, says what it gives the
customer, carries its size (`effort`: setting, days, weeks, quarter) and cost direction (`up`, `neutral`, `down`,
`measure_first`), lists what must be true or confirmed first (`before_deciding`), gives at least one alternative (keeping
and improving the current setup is an alternative), and says how to undo it. Name an AWS service only when it solves a
problem in the evidence or a need stated in the scope; a service that would be nice to have is not a recommendation.
The report shows every recommendation in the same four fields: **current state** (`current`, optional: one sentence in
the words of the evidence; when absent the report prints the observations of the findings in `refs`), **proposal**
(`change` and `benefit`), **quantitative evidence** (the `list_prices` you cite and the figures `benefits.py` computes
from what your `refs` cite: findings by severity, resources touched, single-AZ components, self-managed hosts, pipeline
coverage and history, dates, the account bill; never write `evidence` yourself) and **prerequisites** (`before_deciding`,
`alternatives`, `rollback`). Cite the findings and resources the change touches in `refs`: the evidence rows are
computed from them, so a recommendation that cites one finding shows one finding's worth of evidence.

**Not now.** `not_now` lists what you considered and would not recommend for this workload today, with the reason and what
would change your view. An empty recommendation list with a clear `not_now` is a valid review.

**Unknown demand.** When the evidence cannot show a need (analytics demand, an AI use case, asynchronous steps), mark the
pathway `not_assessed`, and write what it would take to decide: the prerequisite and the two or three questions whose answers
decide it, in `before_deciding` or `not_now`. You may say what the data or services already in scope would make possible,
as something to explore with the owner, never as a promised benefit.

**Language.** Write everything in English (advice, candidates, list-price labels, diagram labels and captions), whatever the report language; the finished report is translated once at the end. The validator refuses text in another language.

**Numbers and wording.** No amounts in the text: cite `list_prices` ids from `list_price.py` (your own file, with your
`--id-prefix`); a price for a size still to be measured is an example (`cost_direction: measure_first`). No delivery
durations or dates you did not read in the evidence (the effort word is the size). No effect percentages without a
measurement. No superlatives or promotional words. Suggest rather than order: "would", "could", "worth considering";
The validator refuses these.

**Stay in your lane.** One change has one owner. Ownership when domains meet: where a component runs and scales (EC2, ECS,
EKS, Lambda, the chatbot host) belongs to `containers`; data stores, their availability, backups, credentials and
connections to `database`; moving data out for analysis to `analytics`; models, guardrails, prompts, model logging and AI
evaluation to `genai`; pipelines, tests, artifacts, deployment safety (circuit breakers, rollbacks), IaC, alarms, logs,
dashboards and tracing to `devops`. Security findings stay with the domain of the resource. When your advice depends on
another domain, add a `handoffs` item (`{"to": "<id>", "text": "..."}`) instead of writing their recommendation.

**Diagram.** One focused drawing of your domain for this workload: the existing components your advice touches
(`retained`, `changed`, `removal_candidate`, with the `resource_ids` or `stack_resources` of the current graph), what you
propose (`proposed`, no resource IDs), and links you could not confirm (`unresolved`). Reuse the labels of
`04-review/diagrams/diagram-graph.json` for existing components and keep only the groups (region, VPC, AZ, subnet) that
explain your point. Write `diagram-graph.json`, then draw it with the command in the brief (`diagram.py --graph`): the
renderer makes the AWS-styled `diagram.aws.svg` with official icons and checks its geometry; you do not redraw it and you
do not need to look at a PNG. Exit code 0 is the check.

**Read-only.** Read the files named in the brief. AWS calls are limited to describe/list/get and `pricing get-products`;
never change the account.

## advice.json

```json
{
  "schema_version": "1.0",
  "specialist": "database",
  "summary": "Two to four sentences: what you see and what you would raise first.",
  "current_state": [{"text": "Aurora cluster ... has one writer in us-east-1b and no reader.", "refs": ["f0064", "e0026"]}],
  "recommendations": [{
    "title": "Give the order database a failover target and a deletion guard",
    "pathway": "managed-database",
    "current": "Cluster ... has one writer in us-east-1b, no reader, and DeletionProtection off.",
    "change": "Add an Aurora reader in us-east-1a to cluster ..., set DeletionProtection and DeletionPolicy: Snapshot in shopnow-data.",
    "benefit": "A writer or zone failure promotes the reader instead of stopping four services; a stack mistake keeps a snapshot.",
    "refs": ["f0064", "m0004"],
    "effort": "days", "cost_direction": "up",
    "list_prices": [{"id": "db-p001", "sign": "+"}],
    "before_deciding": ["Agree RTO and RPO with the owner."],
    "alternatives": ["A smaller reader class or Aurora Serverless v2 reader when cost decides it."],
    "rollback": "Delete the reader; restore the previous template values."
  }],
  "not_now": [{"text": "No evidence supports moving to another engine such as DynamoDB.", "refs": ["e0026"]}],
  "handoffs": [{"to": "devops", "text": "Alarms on replica lag and failover events belong to the alarm set."}],
  "diagram": {"svg": "diagram.aws.svg", "caption": "Data tier today and with the proposed reader, backup plan and proxy."},
  "references": [{"title": "Amazon Aurora high availability", "url": "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html"}]
}
```

`candidates.json` in the same folder holds your pathway objects in the [scoring and output contract](scoring-and-output.md)
(criteria, validation, rollback, owner, cost assumptions, sources and the option list your pathway carries). Its status for
each pathway is your verdict; the synthesis keeps it unless it records `synthesis_note`.
