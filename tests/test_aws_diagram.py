"""The AWS-styled diagram drawn by code (aws_diagram.py through diagram.py): layout, routing, ids, audit and QA."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import run_python  # noqa: E402
from test_platform_rules import load, res, ACCOUNT, REGION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "skills/aws-architecture-review/scripts/diagram.py"
AWS_DIAGRAM = ROOT / "skills/aws-architecture-review/scripts/aws_diagram.py"
diagram = sys.modules.get("diagram_platform") or load("diagram_platform", "skills/aws-architecture-review/scripts/diagram.py")
aws = load("aws_diagram_test", "skills/aws-architecture-review/scripts/aws_diagram.py")
kit = load("report_kit_for_diagram", "skills/aws-architecture-review/scripts/report_kit.py")


def sub(sid, cidr, az, name, public=False):
    return res("ec2.subnet", sid, {"SubnetId": sid, "VpcId": "vpc-1", "CidrBlock": cidr, "AvailabilityZone": az, "MapPublicIpOnLaunch": public,
                                   "Tags": [{"Key": "Name", "Value": name}]}, ["e1"])


def inventory():
    """A small VPC: two AZs with a public and a private subnet each, a NAT gateway, an ALB over both app subnets, an ECS
    service, an Aurora cluster with one writer, security groups, and a pipeline outside the VPC."""
    alb = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/internal/1"
    return {"run_id": "20261101T000000Z", "account_id": ACCOUNT, "regions": [REGION], "services": ["ec2", "elbv2", "ecs", "rds", "codepipeline"],
            "relationships": [
                {"source": f"{ACCOUNT}:{REGION}:rds.instance:writer", "target": f"{ACCOUNT}:{REGION}:rds.cluster:orders", "relation": "member_of", "evidence_ids": ["e1"]},
                {"source": f"{ACCOUNT}:{REGION}:elbv2.load-balancer:{alb}", "target": f"{ACCOUNT}:{REGION}:ec2.security-group:sg-1", "relation": "associated_with", "evidence_ids": ["e1"]},
                {"source": f"{ACCOUNT}:{REGION}:ec2.nat-gateway:nat-1", "target": f"{ACCOUNT}:{REGION}:ec2.internet-gateway:igw-1", "relation": "routes_to", "evidence_ids": ["e1"]},
                {"source": f"{ACCOUNT}:{REGION}:codepipeline.pipeline:shop", "target": f"{ACCOUNT}:{REGION}:ecs.service:arn:svc", "relation": "deploys_to", "evidence_ids": ["e1"]}],
            "resources": [
                res("ec2.vpc", "vpc-1", {"VpcId": "vpc-1", "CidrBlock": "10.70.0.0/16", "Tags": [{"Key": "Name", "Value": "shop-vpc"}]}, ["e1"]),
                sub("subnet-pa", "10.70.0.0/24", "us-east-1a", "public-a", True), sub("subnet-pb", "10.70.1.0/24", "us-east-1b", "public-b", True),
                sub("subnet-a", "10.70.10.0/24", "us-east-1a", "app-a"), sub("subnet-b", "10.70.11.0/24", "us-east-1b", "app-b"),
                res("ec2.nat-gateway", "nat-1", {"NatGatewayId": "nat-1", "SubnetId": "subnet-pa", "VpcId": "vpc-1"}, ["e1"], name="nat-a"),
                res("ec2.internet-gateway", "igw-1", {"InternetGatewayId": "igw-1", "Attachments": [{"VpcId": "vpc-1"}]}, ["e1"], name="igw"),
                res("ec2.security-group", "sg-1", {"GroupId": "sg-1", "VpcId": "vpc-1", "IpPermissions": []}, ["e1"], name="alb-sg"),
                res("ec2.security-group", "sg-2", {"GroupId": "sg-2", "VpcId": "vpc-1", "IpPermissions": []}, ["e1"], name="app-sg"),
                res("elbv2.load-balancer", alb, {"LoadBalancerName": "internal", "Scheme": "internal", "Type": "application", "VpcId": "vpc-1",
                                                 "AvailabilityZones": [{"SubnetId": "subnet-a", "ZoneName": "us-east-1a"}, {"SubnetId": "subnet-b", "ZoneName": "us-east-1b"}]}, ["e1"], name="internal"),
                res("ecs.service", "arn:svc", {"serviceName": "catalog-api", "launchType": "FARGATE", "desiredCount": 2,
                                               "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-a", "subnet-b"]}}}, ["e1"], name="catalog-api"),
                res("rds.cluster", "orders", {"DBClusterIdentifier": "orders", "Engine": "aurora-postgresql", "DBClusterMembers": [{"DBInstanceIdentifier": "writer"}]}, ["e1"], name="orders"),
                res("rds.instance", "writer", {"DBInstanceIdentifier": "writer", "DBInstanceClass": "db.r6g.large", "AvailabilityZone": "us-east-1b",
                                              "DBSubnetGroup": {"VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-a"}, {"SubnetIdentifier": "subnet-b"}]}}, ["e1"], name="writer"),
                res("codepipeline.pipeline", "shop", {"name": "shop"}, ["e1"], name="shop")]}


def ids_in(svg, kind):
    return set(re.findall(rf'data-{kind}-id="([^"]+)"', svg))


class AwsDiagramTests(unittest.TestCase):
    def test_diagram_py_draws_audits_and_checks_geometry_without_an_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inventory.json").write_text(json.dumps(inventory()))
            run = run_python(str(DIAGRAM), "--inventory", str(root / "inventory.json"), "--output", str(root / "diagrams"), "--name", "as-is",
                             "--title", "shop: current architecture", "--icons", str(root / "no-icons-here"), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            out = json.loads(run.stdout)
            self.assertEqual((out["status"], out["coverage"], out["audit"], out["geometry"]), ("drawn", "pass", "pass", "pass"))
            self.assertEqual(out["icons"], "service_tags", "without the companion skill, nodes carry a service tag instead of an icon")
            for name in ("as-is.aws.svg", "as-is.aws.html", "diagram-graph.json", "diagram-qa.json", "diagram-audit.json"):
                self.assertTrue((root / "diagrams" / name).is_file(), name)
            self.assertFalse((root / "diagrams/as-is.d2").exists(), "D2 is opt-in now")
            graph = json.loads((root / "diagrams/diagram-graph.json").read_text())
            svg = (root / "diagrams/as-is.aws.svg").read_text()
            kit.scan_svg(svg)  # embeddable: no scripts, no external references
            self.assertEqual(ids_in(svg, "node"), {n["id"] for n in graph["nodes"]})
            self.assertEqual(ids_in(svg, "group"), {g["id"] for g in graph["groups"]})
            self.assertEqual(ids_in(svg, "edge"), {e["id"] for e in graph["edges"]}, "badge edges keep their id on the chip")
            self.assertIn('data-kind="public-subnet"', svg); self.assertIn('data-kind="subnet"', svg)
            self.assertIn("Public subnet public-a", svg); self.assertIn("Private subnet app-a", svg)
            self.assertIn("NAT gateway nat-a", svg); self.assertIn("ECS service catalog-api", svg)
            self.assertIn(">Legend<", svg); self.assertIn("shop: current architecture", svg)
            self.assertNotIn("<script", svg.lower())
            qa = json.loads((root / "diagrams/diagram-qa.json").read_text())
            self.assertEqual(qa["status"], "pass")
            for check in ("nodes_inside_their_group", "no_node_overlap", "edges_end_at_their_nodes", "connectors_avoid_nodes"):
                self.assertEqual(qa["checks"][check]["status"], "pass", check)
            self.assertEqual(qa["size"]["badge_edges"], 1, "the ALB -> security group attachment is a chip, not a line")
            audit = json.loads((root / "diagrams/diagram-audit.json").read_text())
            self.assertEqual((audit["status"], audit["coverage"]["status"], audit["geometry"]["status"]), ("pass", "pass", "pass"))
            self.assertIn("geometry checked", audit["visual_inspection"])
            # connectors are orthogonal
            for d in re.findall(r'<g data-edge-id="[^"]+"><path d="([^"]+)"', svg):
                pts = [tuple(map(float, p.split())) for p in d[2:].split(" L ")]
                for a, b in zip(pts, pts[1:]):
                    self.assertTrue(abs(a[0] - b[0]) < 0.6 or abs(a[1] - b[1]) < 0.6, f"diagonal segment in {d[:60]}")
            # the audit command still works on the drawing, as before
            check = run_python(str(DIAGRAM), "--audit-graph", str(root / "diagrams/diagram-graph.json"), "--audit-svg", str(root / "diagrams/as-is.aws.svg"),
                               "--inventory", str(root / "inventory.json"), "--output", str(root / "audit"), capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_every_icon_mapping_points_at_a_file_of_the_icon_set(self):
        roots = aws.icon_roots()
        if not roots:
            self.skipTest("aws-diagram-design icons not installed beside the skill or in a client skills folder")
        missing = {k: v[0] for k, v in aws.ICONS.items() if v[0] and not (roots[0] / v[0]).is_file()}
        self.assertEqual(missing, {})
        for service in ("codebuild", "secretsmanager", "sns", "logs", "iam", "cloudwatch"):
            self.assertIsNotNone(aws.icon_for({"service": service})[0], service)

    def test_labels_never_cover_nodes_titles_or_each_other_and_wide_rows_wrap(self):
        """Field case: edge labels sat on group titles, under nodes and on top of each other, and a six-stage DevOps
        view drew 3,600 px wide. Labels now go where nothing else is, or become numbered markers explained under the
        legend, and a row of groups wraps at MAX_ROW_W."""
        groups = [{"id": "region", "label": "Region us-east-1", "kind": "region", "parent": None, "resource_ids": []}]
        nodes, edges = [], []
        for g in range(6):
            groups.append({"id": f"stage{g}", "label": f"Stage {g} with a fairly long title", "kind": "other", "parent": "region", "resource_ids": []})
            for k in range(3):
                nodes.append({"id": f"n{g}{k}", "label": f"Component {g}.{k} with a long name", "detail": "detail text that wraps over two lines",
                              "service": "ecs", "type": "ecs.service", "group": f"stage{g}", "state": "observed", "resource_ids": []})
        for g in range(5):
            for k in range(3):
                edges.append({"id": f"e{g}{k}", "source": f"n{g}{k}", "target": f"n{g + 1}{(k + 1) % 3}", "label": f"connection {g}.{k} carries a long description", "state": "observed_configuration"})
        graph = {"title": "Wide pipeline", "groups": groups, "nodes": nodes, "edges": edges}
        with tempfile.TemporaryDirectory() as tmp:
            qa = aws.render(graph, Path(tmp), "wide", None, None, None)
            self.assertEqual(qa["checks"]["labels_clear"]["status"], "pass", qa["checks"]["labels_clear"])
            self.assertEqual(qa["checks"]["readable_width"]["status"], "pass", qa["size"])
            self.assertLessEqual(qa["size"]["width"], aws.MAX_ROW_W + 2 * aws.MARGIN + 2 * aws.PAD)
            svg = (Path(tmp) / "wide.aws.svg").read_text()
            placed = svg.count('data-label-for="')
            self.assertEqual(placed, len(edges), "every edge label is drawn, inline or as a numbered marker")
            for number in qa["checks"]["labels_clear"]["numbered"]:
                self.assertIn(f">{number}</text>", svg)
            if qa["checks"]["labels_clear"]["numbered"]:
                self.assertIn(">Connections<", svg)

    def test_official_style_nodes_and_crowded_graphs_drawn_as_views(self):
        """Field case: boxed cards with three to four text lines made the drawings hard to read. Nodes are now drawn as in
        the official AWS diagrams (icon, name under it, no box unless the state needs an outline), connectors end at the
        icon and never run through another component, and a graph too large for one readable drawing is cut into views
        along its top-level groups, with the components of the other view shown as small copies."""
        groups = [{"id": "region", "label": "Region us-east-1", "kind": "region", "resource_ids": []},
                  {"id": "vpc", "label": "VPC shop", "kind": "vpc", "parent": "region", "resource_ids": []},
                  {"id": "svc", "label": "Regional services", "kind": "services", "parent": "region", "resource_ids": []}]
        nodes = [{"id": f"v{k}", "label": f"ECS service {k}", "type": "ecs.service", "group": "vpc", "state": "observed", "resource_ids": []} for k in range(14)]
        nodes += [{"id": f"s{k}", "label": f"Pipeline {k}", "type": "codepipeline.pipeline", "group": "svc", "state": "proposed" if k == 0 else "observed", "resource_ids": []}
                  for k in range(14)]
        edges = [{"id": f"in{k}", "source": f"v{k}", "target": f"v{k + 1}", "label": "calls", "state": "observed_configuration"} for k in range(0, 12, 2)]
        edges += [{"id": f"sv{k}", "source": f"s{k}", "target": f"s{k + 1}", "label": "triggers", "state": "observed_configuration"} for k in range(0, 12, 2)]
        edges += [{"id": "deploy", "source": "s0", "target": "v0", "label": "deploys", "state": "proposed"}]
        graph = {"title": "Crowded", "groups": groups, "nodes": nodes, "edges": edges}
        with tempfile.TemporaryDirectory() as tmp:
            qa = aws.render(graph, Path(tmp), "crowded", None, None, None)
            svg = (Path(tmp) / "crowded.aws.svg").read_text()
            self.assertEqual(qa["status"], "pass", qa["checks"])
            self.assertEqual((qa["size"]["views"], qa["checks"]["views"]["count"]), (2, 2))
            self.assertIn('data-views="2"', svg); self.assertIn("View 1: VPC shop", svg); self.assertIn("View 2: Regional services", svg)
            self.assertEqual(re.findall(r'data-node-id="([^"]+)"', svg).count("v0"), 1, "each component is drawn in full once")
            self.assertEqual(ids_in(svg, "node"), {n["id"] for n in nodes})
            self.assertEqual(ids_in(svg, "edge"), {e["id"] for e in edges})
            # a connector to a component of the other view is a chip under the component, not a line across the drawing
            self.assertNotIn("data-stub-of", svg); self.assertNotIn("In other views", svg)
            self.assertEqual(sorted(re.findall(r'data-edge-(?:id|repeat)="deploy" data-view-link="(\d)"', svg)), ["1", "2"], "one chip in each view")
            self.assertEqual(diagram.audit(graph, Path(tmp) / "crowded.aws.svg", require_coverage=False)["status"], "pass",
                             "every graph id appears once; the region and the second chip repeat as data-*-repeat")
            self.assertIn('data-group-repeat="region"', svg); self.assertIn('data-edge-repeat="deploy"', svg)
            self.assertEqual(qa["checks"]["connectors_clear_of_nodes"]["status"], "pass")
            plain = re.search(r'<g data-node-id="v3" data-state="observed"><title>[^<]*</title>(<[^>]+>)', svg).group(1)
            self.assertNotIn("stroke=", plain, "an observed component has no box, only its icon (here the service tile)")
            self.assertRegex(svg, r'<g data-node-id="s0" data-state="proposed"><title>[^<]*</title><rect [^>]*stroke-dasharray', "a proposed one keeps its dashed outline")
            self.assertRegex(svg, r'font-weight="600" fill="#232F3E" text-anchor="middle">ECS service 3<', "the name sits centred under the icon")
        # connectors end on the icon, not on the edge of the text column
        pair = {"groups": [], "nodes": [dict(nodes[0], group=None), dict(nodes[1], group=None)], "edges": [edges[0]]}
        boxes = aws.Layout(pair).run()
        (start, *_, end), _ = aws.route_edges(pair, boxes, set())["in0"]
        sx, sy, sw, sh = aws.icon_box(boxes["v0"]); tx, ty, tw, th = aws.icon_box(boxes["v1"])
        self.assertEqual((round(start[0], 1), round(end[0], 1)), (round(sx + sw, 1), round(tx, 1)))
        self.assertTrue(sy <= start[1] <= sy + sh and ty <= end[1] <= ty + th)
        # a graph that fits one drawing stays one drawing
        small = {"title": "Small", "groups": groups, "nodes": nodes[:6] + nodes[14:18], "edges": [e for e in edges if e["id"] in {"in0", "in2", "sv0"}]}
        with tempfile.TemporaryDirectory() as tmp:
            qa = aws.render(small, Path(tmp), "small", None, None, None)
            self.assertEqual(qa["size"]["views"], 1)
            self.assertNotIn("data-stub-of", (Path(tmp) / "small.aws.svg").read_text())
        # members of a group are ordered along their connectors, so a chain sits side by side
        self.assertEqual(aws.flow_order(["c", "b", "a", "z"], [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]), ["a", "b", "c", "z"])

    def test_grouping_resources_wrap_what_they_apply_to(self):
        """Field request: security groups, target groups and Auto Scaling groups are drawn around the components they apply
        to (a red dashed frame, a purple container, an orange container), not as separate icons joined by lines. The ids
        stay once in the SVG, so the audit passes; the readability record and the wrap rule are part of the QA."""
        g = {"title": "T", "groups": [{"id": "vpc", "label": "VPC v", "kind": "vpc"}, {"id": "sub", "label": "Private subnet app", "kind": "subnet", "parent": "vpc"}],
             "nodes": [{"id": "alb", "label": "ALB internal", "type": "elbv2.load-balancer", "group": "sub"},
                       {"id": "svc", "label": "ECS service api", "type": "ecs.service", "group": "sub"},
                       {"id": "svc2", "label": "ECS service web", "type": "ecs.service", "group": "sub"},
                       {"id": "tg", "label": "Target group api", "type": "elbv2.target-group", "group": "sub"},
                       {"id": "sg", "label": "Security group app-sg", "type": "ec2.security-group", "group": "vpc", "resource_ids": ["x"]},
                       {"id": "asg", "label": "Auto Scaling group web", "type": "autoscaling.group", "group": "sub"},
                       {"id": "i1", "label": "EC2 web 1", "type": "ec2.instance", "group": "sub"}, {"id": "i2", "label": "EC2 web 2", "type": "ec2.instance", "group": "sub"}],
             "edges": [{"id": "e1", "source": "alb", "target": "tg", "label": "listener"}, {"id": "e2", "source": "tg", "target": "svc", "label": "registered"},
                       {"id": "e2b", "source": "tg", "target": "svc2", "label": "registered"},
                       {"id": "e3", "source": "svc", "target": "sg", "label": "Security group attachment", "drawn_as": "badge"},
                       {"id": "e4", "source": "asg", "target": "i1", "label": "member"}, {"id": "e5", "source": "asg", "target": "i2", "label": "member"}]}
        with tempfile.TemporaryDirectory() as tmp:
            qa = aws.render(g, Path(tmp), "wrap", None, None, None)
            svg = (Path(tmp) / "wrap.aws.svg").read_text()
            self.assertEqual(qa["status"], "pass", qa["checks"])
            kinds = dict((k, v) for v, k in re.findall(r'<g (data-[a-z-]+="[^"]+")[^>]*data-kind="(security-group|target-group|autoscaling)"', svg))
            self.assertEqual(kinds, {"target-group": 'data-node-id="tg"', "autoscaling": 'data-node-id="asg"', "security-group": 'data-frame-for="svc"'})
            self.assertEqual(diagram.audit(g, Path(tmp) / "wrap.aws.svg", require_coverage=False)["status"], "pass")
            self.assertEqual(ids_in(svg, "edge"), {"e1", "e2", "e2b", "e3", "e4", "e5"}, "membership edges keep their ids on the container")
            self.assertEqual(svg.count('data-edge-id="e2"'), 1)
            r = qa["readability"]
            for key in ("connectors", "crossings", "bends", "length", "longest_share_of_diagonal", "width", "height"):
                self.assertIn(key, r)
            self.assertEqual((r["connectors"], r["crossings"]), (1, 0), "only the listener is a line; registration and membership are containment")
        # wrapping breaks at spaces first and splits an over-long word only at - _ / without leaving a short piece
        self.assertEqual(aws.wrap("NAT gateway shopnow-nat-a", aws.LABEL_SIZE, aws.TEXT_W, 3)[0], ["NAT gateway", "shopnow-nat-a"])
        lines, _ = aws.wrap("shopnow-data-dbcluster-a7sa5hydmrzh", aws.LABEL_SIZE, 90, 4)
        self.assertTrue(len(lines) > 1 and all(len(line) >= aws.MIN_PIECE for line in lines), lines)
        # the same graph twice gives the same drawing (the arrangement search is deterministic)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            aws.render(g, Path(a), "x", None, None, None); aws.render(g, Path(b), "x", None, None, None)
            self.assertEqual((Path(a) / "x.aws.svg").read_text(), (Path(b) / "x.aws.svg").read_text())

    def test_focused_to_be_graph_draws_states_without_an_inventory(self):
        """A specialist's drawing: retained, changed, proposed and removal-candidate nodes, a proposed edge, no inventory
        to cover. The drawing passes the id audit and tags every non-observed state."""
        graph = {"title": "Data tier with the proposed reader", "groups": [
            {"id": "region", "label": "Region us-east-1", "kind": "region", "parent": None, "resource_ids": []},
            {"id": "vpc", "label": "VPC shop-vpc", "kind": "vpc", "parent": "region", "resource_ids": []},
            {"id": "az-a", "label": "AZ us-east-1a", "kind": "az", "parent": "vpc", "resource_ids": [], "az": "us-east-1a"},
            {"id": "az-b", "label": "AZ us-east-1b", "kind": "az", "parent": "vpc", "resource_ids": [], "az": "us-east-1b"},
            {"id": "db-a", "label": "Private subnet db-a", "kind": "subnet", "parent": "az-a", "resource_ids": [], "public": False},
            {"id": "db-b", "label": "Private subnet db-b", "kind": "subnet", "parent": "az-b", "resource_ids": [], "public": False}],
            "nodes": [
                {"id": "n1", "label": "Aurora writer orders-writer", "detail": "db.r6g.large · the only instance", "type": "rds.instance", "service": "rds", "group": "db-b", "state": "retained", "resource_ids": []},
                {"id": "n2", "label": "Aurora reader (proposed)", "detail": "same class, us-east-1a", "type": "rds.instance", "service": "rds", "group": "db-a", "state": "proposed", "resource_ids": []},
                {"id": "n3", "label": "ECS service order-api", "detail": "connects through the cluster endpoint", "type": "ecs.service", "service": "ecs", "group": "vpc", "state": "changed", "resource_ids": []},
                {"id": "n4", "label": "Shared admin secret", "detail": "replace with per-service users", "type": "secretsmanager.secret", "service": "secretsmanager", "group": "vpc", "state": "removal_candidate", "resource_ids": []}],
            "edges": [
                {"id": "e1", "source": "n3", "target": "n1", "label": "writer endpoint :5432", "state": "observed_configuration", "evidence_ids": ["e1"]},
                {"id": "e2", "source": "n1", "target": "n2", "label": "replication", "state": "proposed", "evidence_ids": []}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "diagram-graph.json").write_text(json.dumps(graph))
            run = run_python(str(DIAGRAM), "--graph", str(root / "diagram-graph.json"), "--output", str(root / "out"), "--name", "diagram",
                             "--icons", str(root / "none"), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            out = json.loads(run.stdout)
            self.assertEqual((out["status"], out["coverage"], out["audit"]), ("drawn", "not_checked", "pass"))
            svg = (root / "out/diagram.aws.svg").read_text()
            for state in ("proposed", "changed", "removal candidate"):
                self.assertIn(f">{state}</text>", svg, f"{state} tag on the node")
            self.assertIn('data-state="retained"', svg)
            self.assertIn("Proposed path", svg, "legend names the proposed edge style")
            self.assertEqual(ids_in(svg, "edge"), {"e1", "e2"})
            # the renderer alone, as a module: wrapping keeps long names inside the box
            lines, truncated = aws.wrap("shopnow-data-dbcluster-a7sa5hydmrzh-very-long-identifier", aws.LABEL_SIZE, 150, 3)
            self.assertTrue(lines and not truncated)
            self.assertTrue(all(aws.text_width(line, aws.LABEL_SIZE) <= 150 for line in lines))
            lines, truncated = aws.wrap(" ".join(["word"] * 60), aws.DETAIL_SIZE, 150, 2)
            self.assertTrue(truncated and lines[-1].endswith("…"))


if __name__ == "__main__":
    unittest.main()
