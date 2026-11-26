# Diagram workflow

For both the current and the proposed architecture: **build the graph, draw it with `diagram.py`, read the audit**. The
drawing is made by code (`aws_diagram.py`): AWS zone containers with the official badges, nodes with the official
service icons, orthogonal connectors, a title and a legend. No agent redraw, no screenshot inspection: the geometry is
checked by the renderer and recorded in `diagram-qa.json`.

## Current state

```bash
python3 "$REVIEW_SKILL/scripts/diagram.py" --inventory "$RUN_DIR/03-as-is-enriched/inventory.json" \
    --output "$RUN_DIR/04-review/diagrams" --name as-is --title "<workload>: current architecture" --language "$LANG"
```

One command writes `diagram-graph.json`, `as-is.aws.svg`, `as-is.aws.html`, `diagram-qa.json` and `diagram-audit.json`
(ids and coverage). Exit code 0 means drawn, audited and geometry-checked; the report is `Final` only on that audit.

The default graph has groups (region > VPC > Availability Zone > subnet, plus regional services) and places each
in-scope resource by its recorded subnet, AZ or VPC. Security groups, route tables, target groups, log groups, alarms and
rules are aggregate nodes that keep every ID; volumes join their instance. Architectural CloudFormation resources the
inventory does not list (ElastiCache, ECR, pipelines, secrets, node groups...) become nodes with `stack_resources`. Node
labels name the type and the resource ("NAT gateway shopnow-nat-a", "ECS service order-api"); the detail line carries
the class, version or placement the collector saw; edges come from the inventory's relationships.

**Edit the graph only when the drawing would otherwise mislead.** The default is complete and honest. Edit
`diagram-graph.json` when roles, ports or declared paths matter for the review: relabel nodes with their role ("order API",
"image bucket"), add the listener and target-group paths, add edges declared in templates or source as `state:
"declared"`, mark links you could not confirm `unresolved`. Keep every resource ID and every subnet/AZ/VPC group, then
redraw with `--graph FILE --inventory ...`; the helper stops when the graph leaves collected resources out. Write labels
in the run's output language; the renderer's own text (title, legend) is localized with the report.

**Completeness is enforced.** Everything collected in scope must be drawn, as its own node or inside an aggregate or
group. `diagram-audit.json` fails when any in-scope resource, subnet, AZ, VPC or architectural stack resource is
missing, and the unified report stays a draft until it passes.

A connection declared in source is not observed traffic; unverified links stay `unresolved`. Do not invent traffic
arrows or make up topology. If workload relationships remain unavailable after enrichment, say so in the title
("inventory overview") and name the missing evidence.

## How the drawing is made

- Region: VPCs side by side, then the regional services, then loose nodes. VPC: Availability Zones as columns with
  their subnets stacked (public first), then one band per group of resources deployed across several subnets, then
  VPC-level components (internet and VPN gateways on top, endpoints and aggregates below). Other groups: a grid whose
  members follow their connectors, so a chain of components sits side by side.
- Nodes are drawn as in the official AWS diagrams: the 48 px icon, the name centred under it and at most two short
  detail lines, no box. Connectors end at the icon and never run through another component or a group title.
- A graph with more than 24 components, or whose layout is larger than 1,500 x 1,400 px, is drawn as two or three
  views in the same SVG, one under the other ("View 1: VPC ...", "View 2: Regional services"), cut along the top-level
  groups at the place that leaves the fewest connectors between views. A connector to a component of another view is
  a chip under the component ("→ Secrets Manager · View 2"), not a line across the drawing. When every cut would leave
  more than 30% of the connectors between views, the graph stays one drawing. `diagram-qa.json` records the views.
- Grouping resources wrap what they apply to: a security group attachment is a red dashed frame around the attached
  component, a target group whose registered components sit in one zone is a purple dashed container around them, an
  Auto Scaling group or EKS node group is an orange dashed container around its instances. The membership edges are
  drawn by the containment and keep their ids on the container; connectors to the target group end at its border.
- The arrangement is searched, not fixed: column counts and member order (barycentre of the connected components) are
  tried and the arrangement with the fewest crossings, bends and the shortest connectors wins; the result is the same
  for the same graph. Names wrap at spaces first and split an over-long identifier only at - _ / without leaving a
  piece shorter than four characters. `diagram-qa.json` `readability` records connectors, crossings, bends, total and
  longest length and the drawing size.
- Zones follow the official AWS convention (region and AZ dashed teal, VPC purple, private subnet teal, public subnet
  green, badge top-left). Node states: `observed`/`retained` plain (icon only), `declared` dashed grey, `changed` orange, `proposed`
  orange dashed, `removal_candidate` red dashed, `unresolved` grey dashed; every non-observed state carries a tag and the
  legend explains it. Edge states: `observed_configuration`, `declared`, `proposed`, `unresolved`.
- Attachments to an aggregate route-table node are chips on the source node ("RT"), not lines; the edge keeps its id on
  the chip.
- Icons come from the aws-diagram-design companion skill (`assets/aws-icons`, found beside this skill or in the
  client's skills folder; `--icons DIR` or `MODA_AWS_ICONS` to point elsewhere). Without it, nodes carry a coloured
  service tag; `diagram-qa.json` records which.
- `diagram-qa.json` checks: every node inside its group, no two nodes overlap, every connector ends at its nodes and
  runs through no other node, edge labels sit beside their own line where no other line crosses (otherwise a numbered
  marker explained under "Connections"), every label fits (long labels are wrapped, then shortened with "…" and listed under `notes`). A failing
  check fails the command; fix the graph (shorter labels, fewer nodes per group) rather than the drawing.
- `--d2` additionally writes the D2 source and renders it with `d2` when installed, as a structural cross-check. It is
  not needed for the deliverable.

Look at the drawing when a reader will (open `as-is.aws.html`); do not spend model turns on screenshots when the QA
passed. If something looks wrong, the fix is in the graph or in `aws_diagram.py`, not in a hand-edited SVG.

## Proposed state

Specialist SA Agents write their own focused graphs (`05-modernization/specialists/<id>/diagram-graph.json`, states
`retained`/`changed`/`proposed`/`removal_candidate`/`unresolved`) and draw them with the same command
(`--graph ... --name diagram`, no inventory: the audit checks ids only). The combined target architecture
(`05-modernization/diagrams/runtime/to-be.aws.svg`) is drawn from the merged proposed graph the same way. Mark new
proposed nodes and retained existing components explicitly; do not present proposal components as observed resources.

**Span groups.** A resource deployed in several subnets (one per AZ: ALBs, ECS services with `awsvpcConfiguration`,
Lambda in a VPC, EKS clusters) is placed in a group of kind `subnet-span` named after those subnets, with `subnet_ids`
and `availability_zones` on the group. It is drawn as a band under the AZ columns. Never label it as a VPC-level or
subnet-less resource: it is inside those subnets, in every listed AZ.

**Pods and other IP targets.** A target group of type `ip` that no ECS service owns becomes a node `Kubernetes pods
behind <target group> × N` (or `IP targets of …`) placed in the subnets its target IPs fall into (kind
`elbv2.ip-targets`, `target_group` on the node). The ECS service node already stands for the tasks registered in its
own target groups.
