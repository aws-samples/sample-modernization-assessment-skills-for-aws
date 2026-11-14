# Required two-stage diagram workflow

For both current and proposed architecture: **render D2, inspect structure, redraw with aws-diagram-design, inspect final images**.

## Evidence graph and initial D2

```bash
python3 "$REVIEW_SKILL/scripts/diagram.py" --inventory "$RUN_DIR/03-as-is-enriched/inventory.json" --output "$RUN_DIR/04-review/diagrams" --name as-is
```

The default graph has groups (region > VPC > Availability Zone > subnet, plus regional services) and places each in-scope resource by its recorded subnet, AZ or VPC. Security groups, route tables, target groups, log groups, alarms and rules are aggregate nodes that keep every ID; volumes join their instance. Architectural CloudFormation resources the inventory does not list (ElastiCache, ECR, pipelines, secrets, node groups...) become nodes with `stack_resources`. Start the workload graph from this one: relabel, regroup and add declared paths, but keep every resource ID and every subnet/AZ/VPC group. Pass an edited graph with `--graph FILE --inventory ...`; the helper stops when the graph leaves collected resources out.

**Completeness is enforced.** Everything collected in scope must be drawn, as its own node or inside an aggregate or group. `diagram-audit.json` fails when any in-scope resource, subnet, AZ, VPC or architectural stack resource is missing, and the unified report stays a draft until it passes.

Use the current-state workload map to show application roles, relevant ingress and
listener rules, data dependencies, actual compute/database AZ placement, shared
egress and boundaries. Show unused AZ capacity when it explains resilience risk.
Separate unrelated tooling; keep shared dependencies. Use template/source labels
for declared runtime names and versions. A connection declared in source is not
observed traffic. Unverified links stay marked unresolved.

Split overview and detail when needed. If workload relationships remain
unavailable after enrichment, label the diagram as an inventory overview and name
the missing evidence. Do not invent traffic arrows or make up topology to satisfy
the diagram requirement.

A graph contains title, nodes (id, label, service, state, resource_ids) and edges (id, source, target, label, state, evidence_ids where observed). Mark new proposed nodes and retained existing components explicitly. Source/target IDs must resolve. Do not present proposal components as observed resources.

Require rendered status and exit code zero in d2-render.json; D2 may leave SVG files on failure. Render account information locally, never in public playgrounds.

## Final AWS redraw

Use the run's resolved output language for human-facing titles, labels, descriptions
and legends. Preserve graph IDs, service identifiers, ports and numeric evidence.
English initial D2 sources may remain canonical, but localized final SVG/HTML and
regenerated PNGs must agree. Add appropriate CJK/RTL typography and repeat visual QA
after localization. Follow the [language policy](../../aws-mcp-setup/references/output-language.md).

Read the installed aws-diagram-design skill and its style guide, relevant architecture/current-state type, AWS-icon primitive, output specification and export reference. Preserve graph content and groups, but redesign the layout rather than recoloring D2 output.

Use bundled official AWS icons with original proportions/colors, AWS white/squid-ink/orange styling and Ember or a disclosed fallback. Select a readable size and split overview/detail when needed. Describe aggregation/fidelity. Draw every group as a boundary (subnets inside AZ columns inside the VPC) and preserve graph IDs in the final SVG as data-group-id, data-node-id and data-edge-id attributes. Verify title/description, accessible SVG semantics, labels, orthogonal routes and legend.

This assessment requires HTML/SVG/PNG exports:

```bash
python3 "$DIAGRAM_SKILL/scripts/self_check.py" as-is.aws.html
python3 "$DIAGRAM_SKILL/scripts/verify-geometry.py" as-is.aws.html
python3 "$REVIEW_SKILL/scripts/diagram.py" --audit-graph diagram-graph.json --audit-svg as-is.aws.svg \
    --inventory "$RUN_DIR/03-as-is-enriched/inventory.json" --output . --name as-is
```

ID correspondence does not prove semantics or visual quality. Open final renders and inspect clipping, overlap, icons, unsupported arrows and observed/proposed distinctions. Confirm that a reader can identify the workload's components, request/data paths where evidenced, failure boundaries and unresolved links. Check both the standalone diagram and its embedded report size. Record visual inspection separately in diagram-audit.json. Link D2 source, initial SVG, final HTML/SVG/PNG and QA. Missing tools or visual QA leave diagrams incomplete; source-only output is not a successful render.

**Span groups.** A resource deployed in several subnets (one per AZ: ALBs, ECS services with `awsvpcConfiguration`, Lambda in a VPC, EKS clusters) is placed in a group of kind `subnet-span` named after those subnets, e.g. `Subnets shopnow-app-a · shopnow-app-b (us-east-1a, us-east-1b)`, with `subnet_ids` and `availability_zones` on the group. Draw it inside the VPC spanning the AZ columns at the row of those subnets, or as a band whose label names the subnets. Never label it as a VPC-level or subnet-less resource: it is inside those subnets, in every listed AZ.

**Pods and other IP targets.** A target group of type `ip` that no ECS service owns becomes a node `Kubernetes pods behind <target group> × N` (or `IP targets of …`) placed in the subnets its target IPs fall into (kind `elbv2.ip-targets`, `target_group` on the node). Pods run on the EKS nodes in those same subnets: draw them inside or beside the node(s) of the matching AZ, under the EKS node group, never at VPC level. The ECS service node already stands for the tasks registered in its own target groups.
