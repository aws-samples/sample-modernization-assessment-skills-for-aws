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
