#!/usr/bin/env python3
"""Build the evidence graph for the current-state diagram, draw it in AWS style, and audit the drawing.

The graph has groups (region > VPC > Availability Zone > subnet, plus a group for regional services) and
nodes placed by their recorded subnet, AZ or VPC. Every in-scope inventory resource and every architectural
CloudFormation resource of the workload must belong to a node or group; aggregation is allowed
("security groups x 11" keeps all eleven IDs), omission is not. The audit fails when:

- an in-scope resource, subnet, AZ or VPC is missing from the graph (coverage), or
- the final SVG lacks a data-node-id / data-group-id / data-edge-id for any graph node, group or edge.

The drawing (<name>.aws.svg / .aws.html) is made by aws_diagram.py from the graph: AWS zone containers with the
official badges, nodes with the official service icons (from the aws-diagram-design companion skill when it is
installed), orthogonal connectors, a title and a legend; its geometry checks are written to diagram-qa.json and the
audit (ids and coverage) to diagram-audit.json in the same run. No agent redraw is needed. `--d2` additionally writes the
D2 source and its render as a structural cross-check when d2 is installed.

  diagram.py --inventory SNAP/inventory.json [--scope SNAP/scope-resources.json] --output DIR      # build + draw + audit
  diagram.py --graph graph.json [--inventory ... --scope ...] --output DIR                           # agent graph
  diagram.py --audit-graph graph.json --audit-svg final.svg --inventory ... [--scope ...] --output DIR
"""
import argparse
from collections import defaultdict
import hashlib
import ipaddress
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_kit import scan_svg  # noqa: E402  (shared, standard library only)
import aws_diagram  # noqa: E402  (same folder)

# Account-wide settings and governance trails are not workload architecture.
NOT_DRAWN = {"iam.account-summary", "s3.account-public-access-block", "cloudtrail.trail", "ssm.managed-instance",
             "autoscaling.launch-configuration", "discovery.tagged-resources"}


# CloudFormation types that are workload components even when the inventory does not list them.
ARCH_TYPES = {
    "AWS::ElastiCache::ReplicationGroup", "AWS::ElastiCache::CacheCluster", "AWS::ElastiCache::ServerlessCache",
    "AWS::ECR::Repository", "AWS::CodePipeline::Pipeline", "AWS::CodeBuild::Project", "AWS::CodeCommit::Repository",
    "AWS::SecretsManager::Secret", "AWS::EKS::Nodegroup", "AWS::SQS::Queue", "AWS::Lambda::Function",
    "AWS::DynamoDB::Table", "AWS::Kinesis::Stream", "AWS::StepFunctions::StateMachine", "AWS::ApiGateway::RestApi",
    "AWS::ApiGatewayV2::Api", "AWS::CloudFront::Distribution", "AWS::EFS::FileSystem", "AWS::OpenSearchService::Domain",
    "AWS::MSK::Cluster", "AWS::Cognito::UserPool", "AWS::CloudWatch::Dashboard", "AWS::Bedrock::Agent",
    "AWS::Bedrock::KnowledgeBase", "AWS::AutoScaling::AutoScalingGroup",
}
# Default node labels by resource type (the agent's edited graph may say more: roles, ports, versions).
TYPE_LABEL = {"ec2.instance": "EC2 instance", "ec2.nat-gateway": "NAT gateway", "ec2.internet-gateway": "Internet gateway", "ec2.vpc-endpoint": "VPC endpoint",
              "ec2.vpn-gateway": "VPN gateway", "ec2.transit-gateway": "Transit gateway", "rds.instance": "RDS instance", "rds.cluster": "RDS cluster",
              "elbv2.load-balancer": "Load balancer", "ecs.service": "ECS service", "ecs.cluster": "ECS cluster", "eks.cluster": "EKS cluster",
              "lambda.function": "Lambda function", "s3.bucket": "S3 bucket", "sns.topic": "SNS topic", "sqs.queue": "SQS queue", "dynamodb.table": "DynamoDB table",
              "elasticache.replication-group": "ElastiCache group", "elasticache.cluster": "ElastiCache cluster", "codepipeline.pipeline": "CodePipeline",
              "codebuild.project": "CodeBuild project", "codecommit.repository": "CodeCommit repository", "efs.file-system": "EFS file system",
              "opensearch.domain": "OpenSearch domain", "kinesis.stream": "Kinesis stream", "stepfunctions.state-machine": "Step Functions state machine",
              "apigateway.rest-api": "API Gateway", "apigatewayv2.api": "API Gateway", "cloudfront.distribution": "CloudFront distribution",
              "cognito.user-pool": "Cognito user pool", "kafka.cluster": "MSK cluster", "redshift.cluster": "Redshift cluster", "bedrock.agent": "Bedrock agent",
              "bedrock.knowledge-base": "Bedrock knowledge base", "sagemaker.endpoint": "SageMaker endpoint", "cloudwatch.dashboard": "CloudWatch dashboard"}
# Default edge labels for the inventory's relationship names.
RELATION_LABEL = {"member": "cluster member", "member_of": "member of", "associated_with": "associated with", "registered_target": "registered target", "routes_to": "routes to",
                  "configured_target": "configured target", "configured_dead_letter": "dead-letter queue",
                  "attached_to": "attached to", "targets": "targets", "listens_on": "listener", "uses": "uses", "writes_to": "writes to", "reads_from": "reads from",
                  "triggers": "triggers", "deploys_to": "deploys to", "publishes_to": "publishes to", "subscribes_to": "subscribes to", "logs_to": "logs to"}
AGGREGATE = {"ec2.security-group": "Security groups", "ec2.route-table": "Route tables", "elbv2.target-group": "Target groups",
             "logs.group": "Log groups", "cloudwatch.alarm": "CloudWatch alarms", "events.rule": "EventBridge rules",
             "ec2.flow-log": "VPC flow logs", "ec2.elastic-ip": "Elastic IPs", "acm.certificate": "ACM certificates",
             "wafv2.web-acl": "WAF Web ACLs", "kms.alias": "KMS keys", "secretsmanager.secret": "Secrets",
             "ecr.repository": "ECR repositories", "autoscaling.group": "Auto Scaling groups", "glue.database": "Glue databases",
             "glue.job": "Glue jobs", "iot.thing": "IoT things", "backup.plan": "Backup plans", "backup.vault": "Backup vaults"}


def name_tag(resource):
    for tag in resource["attributes"].get("Tags") or []:
        if isinstance(tag, dict) and tag.get("Key") == "Name":
            return tag.get("Value")
    return None


def load_scope(path):
    if not path or not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def required(inventory, scope):
    """(resource IDs, stack resource keys) that the drawing must cover."""
    ids = {r["id"] for r in inventory["resources"]} if scope is None else {x["id"] for x in scope.get("resources", [])}
    by_id = {r["id"]: r for r in inventory["resources"]}
    ids = {i for i in ids if i in by_id and by_id[i]["type"] not in NOT_DRAWN and by_id[i].get("level") != "account"}
    stack = set()
    if scope:
        physical = {r["native_id"] for r in inventory["resources"] if r["id"] in ids}
        for item in scope.get("stack_resources", []):
            if item.get("ResourceType") in ARCH_TYPES and item.get("PhysicalResourceId") not in physical:
                stack.add(f"{item['StackName']}/{item['LogicalResourceId']}")
    return ids, stack


def graph_from_inventory(inventory, scope=None):
    ids, stack_keys = required(inventory, scope)
    resources = [r for r in inventory["resources"] if r["id"] in ids]
    by_native = {r["native_id"]: r for r in resources}
    subnets = {r["native_id"]: r for r in resources if r["type"] == "ec2.subnet"}
    groups, nodes, place = {}, [], {}

    def group(gid, label, kind, parent=None, resource_ids=()):
        if gid not in groups:
            groups[gid] = {"id": gid, "label": label, "kind": kind, "parent": parent, "resource_ids": []}
        groups[gid]["resource_ids"] = sorted(set(groups[gid]["resource_ids"]) | set(resource_ids))
        return gid

    regions = sorted({r["region"] for r in resources if r["region"] not in {"global", "unknown"}} or set(inventory["regions"]))
    for region in regions:
        group(f"region-{region}", f"Region {region}", "region")
    for vpc in [r for r in resources if r["type"] == "ec2.vpc"]:
        cidr = vpc["attributes"].get("CidrBlock", "")
        group(vpc["native_id"], f"VPC {name_tag(vpc) or vpc['native_id']} {cidr}".strip(), "vpc", f"region-{vpc['region']}", [vpc["id"]])
    for sub in subnets.values():
        a = sub["attributes"]
        vpc, az = a.get("VpcId"), a.get("AvailabilityZone")
        parent = vpc if vpc in groups else group(vpc, f"VPC {vpc}", "vpc", f"region-{sub['region']}")
        az_id = group(f"az-{vpc}-{az}", f"AZ {az}", "az", parent)
        groups[az_id]["az"] = az
        public = bool(a.get("MapPublicIpOnLaunch"))
        gid = group(sub["native_id"], f"{'Public' if public else 'Private'} subnet {name_tag(sub) or sub['native_id']} {a.get('CidrBlock', '')}".strip(), "subnet", az_id, [sub["id"]])
        groups[gid]["public"] = public
    services_group = {region: group(f"services-{region}", "Regional services", "services", f"region-{region}") for region in regions}

    def vpc_group(vpc, region):
        return vpc if vpc and vpc in groups else services_group.get(region, f"region-{region}")

    def subnet_or_vpc(subnet_ids, vpc, region):
        found = sorted(s for s in set(subnet_ids) if s in subnets)
        if len(found) == 1:
            return found[0]
        if found:
            # Deployed in several subnets (one per AZ): a span group inside the VPC, named after those subnets, so the
            # drawing says "in app-a and app-b" rather than dropping the resource to VPC level as if it had no subnet.
            # Subnet resource ids are kept in `subnet_ids`, not `resource_ids`, because the audit expects every subnet
            # to be drawn by its own subnet group.
            names = [name_tag(subnets[s]) or s for s in found]
            zones = sorted({subnets[s]["attributes"].get("AvailabilityZone") or "" for s in found} - {""})
            gid = group("span-" + "-".join(found), f"Subnets {' · '.join(names)} ({', '.join(zones)})", "subnet-span",
                        vpc_group(subnets[found[0]]["attributes"].get("VpcId"), region))
            groups[gid]["subnet_ids"] = found
            groups[gid]["availability_zones"] = zones
            return gid
        return vpc_group(vpc, region)

    attached = defaultdict(list)
    for r in resources:
        if r["type"] == "ec2.volume":
            for att in r["attributes"].get("Attachments", []):
                if att.get("InstanceId") in by_native:
                    attached[att["InstanceId"]].append(r["id"])
    aggregates = defaultdict(list)
    for r in resources:
        a, kind, region = r["attributes"], r["type"], r["region"]
        if kind in {"ec2.vpc", "ec2.subnet"} or (kind == "ec2.volume" and any(r["id"] in v for v in attached.values())):
            continue
        if kind in AGGREGATE:
            where = vpc_group(a.get("VpcId"), region) if a.get("VpcId") else services_group.get(region)
            aggregates[(kind, where)].append(r["id"])
            continue
        detail = ""
        if kind == "ec2.instance":
            where = subnet_or_vpc([a.get("SubnetId")], a.get("VpcId"), region)
            detail = a.get("InstanceType", "")
        elif kind == "ec2.nat-gateway":
            where = subnet_or_vpc([a.get("SubnetId")], a.get("VpcId"), region)
        elif kind == "rds.instance":
            group_subnets = [s.get("SubnetIdentifier") for s in (a.get("DBSubnetGroup") or {}).get("Subnets", []) if isinstance(s, dict)]
            in_az = [s for s in group_subnets if s in subnets and subnets[s]["attributes"].get("AvailabilityZone") == a.get("AvailabilityZone")]
            where = subnet_or_vpc(in_az[:1], (a.get("DBSubnetGroup") or {}).get("VpcId"), region)
            detail = f"{a.get('DBInstanceClass', '')} {a.get('AvailabilityZone', '')}".strip()
        elif kind == "elbv2.load-balancer":
            where = subnet_or_vpc([z.get("SubnetId") for z in a.get("AvailabilityZones", [])], a.get("VpcId"), region)
            detail = f"{a.get('Scheme', '')} · {', '.join(z.get('ZoneName', '') for z in a.get('AvailabilityZones', []))}"
        elif kind == "ecs.service":
            net = ((a.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {})
            where = subnet_or_vpc(net.get("subnets") or [], None, region)
            placed = [name_tag(subnets[x]) or x for x in (net.get("subnets") or []) if x in subnets]
            detail = f"{a.get('launchType') or 'capacity provider'} · desired {a.get('desiredCount')}" + (f" · subnets {', '.join(placed)}" if placed else "")
        elif kind == "eks.cluster":
            vpc_cfg = a.get("resourcesVpcConfig") or {}
            where = subnet_or_vpc(vpc_cfg.get("subnetIds") or [], vpc_cfg.get("vpcId"), region)
            detail = f"Kubernetes {a.get('version', '')}"
        elif kind == "rds.cluster":
            member = next((by_native.get(m.get("DBInstanceIdentifier")) for m in a.get("DBClusterMembers", [])), None)
            vpc = ((member or {}).get("attributes", {}).get("DBSubnetGroup") or {}).get("VpcId")
            where = vpc_group(vpc, region)
            detail = a.get("Engine", "")
        elif kind in {"ec2.internet-gateway"}:
            where = vpc_group(next((x.get("VpcId") for x in a.get("Attachments", [])), None), region)
        elif kind == "ec2.vpc-endpoint":
            where = vpc_group(a.get("VpcId"), region)
        elif kind == "lambda.function":
            where = subnet_or_vpc((a.get("VpcConfig") or {}).get("SubnetIds") or [], (a.get("VpcConfig") or {}).get("VpcId"), region)
        elif kind == "ecs.cluster":
            where = services_group.get(region)
        else:
            where = services_group.get(region, f"region-{region}")
        node_ids = [r["id"]] + attached.get(r["native_id"], [])
        label = f"{TYPE_LABEL.get(kind, kind.split('.')[0].upper())} {name_tag(r) or r['name']}"
        nodes.append({"id": f"n{len(nodes) + 1}", "label": label, "detail": detail, "service": kind.split(".")[0], "type": kind,
                      "region": region, "group": where, "state": "observed", "resource_ids": node_ids})
    for (kind, where), members in sorted(aggregates.items()):
        nodes.append({"id": f"n{len(nodes) + 1}", "label": f"{AGGREGATE[kind]} × {len(members)}", "detail": "aggregated",
                      "service": kind.split(".")[0], "type": kind, "group": where, "state": "observed", "resource_ids": sorted(members)})
    # IP targets that no ECS service owns are pods (ALB controller) or other VPC workloads: their health records give
    # the IP and AZ, and the IP falls into a subnet CIDR, so they are drawn in those subnets next to the nodes that run
    # them rather than floating at VPC level. ECS-owned target groups are already represented by the service node.
    owned = {lb.get("targetGroupArn") for r in resources if r["type"] == "ecs.service" for lb in r["attributes"].get("loadBalancers") or []}
    networks = []
    for sid, sub in subnets.items():
        try:
            networks.append((ipaddress.ip_network(sub["attributes"].get("CidrBlock", "")), sid))
        except ValueError:
            continue
    pod_edges = []
    for tg in [r for r in resources if r["type"] == "elbv2.target-group" and r["attributes"].get("TargetType") == "ip" and r["native_id"] not in owned]:
        health = ((tg["observations"].get("target_health") or {}).get("data") or {}).get("TargetHealthDescriptions") or []
        placed, states, zones = [], [], set()
        for t in health:
            address = (t.get("Target") or {}).get("Id")
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            placed += [sid for net, sid in networks if ip in net]
            states.append(((t.get("TargetHealth") or {}).get("State")) or "unknown")
            zones.add((t.get("Target") or {}).get("AvailabilityZone") or "")
        if not placed:
            continue
        name = tg["name"]
        kubernetes = name.startswith("k8s-")
        label = (f"Kubernetes pods behind {name}" if kubernetes else f"IP targets of {name}") + f" × {len(health)}"
        healthy = sum(1 for x in states if x == "healthy")
        detail = f"port {tg['attributes'].get('Port')} · {healthy}/{len(health)} healthy · {', '.join(sorted(z for z in zones if z))}" + (" · ALB controller target group" if kubernetes else "")
        node = {"id": f"n{len(nodes) + 1}", "label": label, "detail": detail, "service": "eks" if kubernetes else "elbv2", "type": "elbv2.ip-targets",
                "group": subnet_or_vpc(placed, None, tg["region"]), "state": "observed", "resource_ids": [], "target_group": tg["native_id"]}
        nodes.append(node)
        pod_edges.append((tg["id"], node["id"], tg["evidence_ids"]))
    if stack_keys:
        types = {f"{x['StackName']}/{x['LogicalResourceId']}": x["ResourceType"] for x in scope.get("stack_resources", [])}
        by_type = defaultdict(list)
        for key in sorted(stack_keys):
            by_type[types[key]].append(key)
        region = regions[0]
        for cfn_type, keys in sorted(by_type.items()):
            label = cfn_type.split("::", 1)[1].replace("::", " ")
            nodes.append({"id": f"n{len(nodes) + 1}", "label": f"{label}{' × ' + str(len(keys)) if len(keys) > 1 else ''}",
                          "detail": "from CloudFormation", "service": cfn_type.split("::")[1].lower(), "type": cfn_type,
                          "group": services_group[region], "state": "declared", "resource_ids": [], "stack_resources": keys})
    if scope:
        account_for_stack_resources(nodes, groups, resources, scope, services_group, regions[0])
    index = {rid: n["id"] for n in nodes for rid in n["resource_ids"]}
    for g in groups.values():
        for rid in g["resource_ids"]:
            index.setdefault(rid, g["id"])
    node_ids = {n["id"] for n in nodes}
    edges = {}
    for rel in inventory.get("relationships", []):
        source, target = index.get(rel["source"]), index.get(rel["target"])
        if not source or not target or source == target or rel["relation"] == "contained_in":
            continue
        if source not in node_ids or target not in node_ids:
            continue
        key = (source, target, rel["relation"])
        edge = edges.setdefault(key, {"id": f"edge{len(edges) + 1}", "source": source, "target": target, "label": RELATION_LABEL.get(rel["relation"], rel["relation"].replace("_", " ")),
                                      "state": "observed_configuration", "evidence_ids": []})
        edge["evidence_ids"] = sorted(set(edge["evidence_ids"]) | set(rel["evidence_ids"]))
    for tg_id, node_id, evidence in pod_edges:
        source = index.get(tg_id)
        if source and source in node_ids:
            key = (source, node_id, "registered_target")
            edges.setdefault(key, {"id": f"edge{len(edges) + 1}", "source": source, "target": node_id, "label": "registered target",
                                   "state": "observed_configuration", "evidence_ids": sorted(set(evidence))})
    used = {n["group"] for n in nodes} | {g["parent"] for g in groups.values() if g["parent"]}
    group_list = [g for g in groups.values() if g["id"] in used or g["resource_ids"]]
    return {"title": "Current architecture from observed configuration",
            "aggregation": "Nodes are placed by recorded subnet, AZ or VPC; aggregate nodes keep every resource ID. Edges are configured relationships, not traffic.",
            "groups": group_list, "nodes": nodes, "edges": list(edges.values())}


IDENTITY_TYPES = {"AWS::IAM::Role", "AWS::IAM::ManagedPolicy", "AWS::IAM::Policy", "AWS::IAM::InstanceProfile", "AWS::IAM::User", "AWS::IAM::Group"}


def account_for_stack_resources(nodes, groups, resources, scope, services_group, region):
    """Every CloudFormation resource of the workload ends up attached to a node: identities become one aggregate node,
    sub-resources (task definitions, listeners, routes, subnet groups, EKS add-ons...) are recorded on the node of the
    parent they configure. The audit then reports which stack resources, if any, no node accounts for."""
    # (identifier, inventory type) -> resource id. Both an ECS and an EKS cluster can be called "shopnow", so a
    # CloudFormation type only matches inventory records of the same service.
    by_native = defaultdict(list)
    for r in resources:
        keys = [r["native_id"]] + [r["attributes"][k] for k in ("Arn", "FunctionArn", "TopicArn", "QueueArn", "RepositoryArn", "LoadBalancerArn",
                                                              "TargetGroupArn", "DBClusterArn", "PublicIp") if isinstance(r["attributes"].get(k), str)]
        for k in keys:
            by_native[k].append(r)
    service_of = {"ElasticLoadBalancingV2": "elbv2", "ElasticLoadBalancing": "elb", "Logs": "logs", "SecretsManager": "secretsmanager", "ElastiCache": "elasticache"}

    def resolve(phys, cfn):
        service = cfn.split("::")[1] if cfn.count("::") == 2 else ""
        prefix = service_of.get(service, service.lower()) + "."
        candidates = list(by_native.get(phys, []))
        if phys and len(phys) > 6:  # names inside ARNs: cluster/shopnow, repository/x, function:name
            candidates += [r for k, rs in by_native.items() if k.endswith("/" + phys) or k.endswith(":" + phys) for r in rs]
        typed = [r for r in candidates if r["type"].startswith(prefix)]
        chosen = typed or ([] if service else candidates)
        return chosen[0]["id"] if chosen else None
    node_of = {rid: n for n in nodes for rid in n["resource_ids"]}
    resources_by_id = {r["id"]: r for r in resources}
    node_by_type = defaultdict(list)
    for n in nodes:
        node_by_type[n["type"]].append(n)
    accounted = {k for n in nodes for k in n.get("stack_resources", [])}
    identities = []

    def attach(node, key):
        node.setdefault("stack_resources", [])
        if key not in node["stack_resources"]:
            node["stack_resources"].append(key)
        accounted.add(key)

    def find(predicate, kinds):
        for kind in kinds:
            for n in node_by_type.get(kind, []):
                if predicate(n):
                    return n
        return None

    for item in scope.get("stack_resources", []):
        key, cfn, phys = f"{item['StackName']}/{item['LogicalResourceId']}", item.get("ResourceType", ""), str(item.get("PhysicalResourceId") or "")
        if key in accounted:
            continue
        rid = resolve(phys, cfn)
        if rid and rid in node_of:
            attach(node_of[rid], key)
            continue
        if cfn in IDENTITY_TYPES:
            identities.append((key, cfn))
            continue
        owner = None
        if cfn in {"AWS::ECS::TaskDefinition"}:
            family = phys.rsplit(":", 1)[0]
            owner = find(lambda n: any(str(resources_by_id[r]["attributes"].get("taskDefinition", "")).startswith(family) for r in n["resource_ids"]), ["ecs.service"])
        elif cfn.startswith("AWS::ElasticLoadBalancingV2::Listener"):
            parts = phys.split(":listener")[-1].split("/")  # /app/<name>/<lb-id>/<listener-id>[/<rule-id>]
            lb_key = "/".join(parts[1:4]) if len(parts) >= 4 else None
            owner = find(lambda n: lb_key and any(lb_key in r for r in n["resource_ids"]), ["elbv2.load-balancer"])
        elif cfn in {"AWS::EC2::Route", "AWS::EC2::SubnetRouteTableAssociation"}:
            rtb = phys.split("|")[0] if cfn == "AWS::EC2::Route" else None
            owner = find(lambda n: rtb is None or any(rtb in r for r in n["resource_ids"]), ["ec2.route-table"])
        elif cfn == "AWS::EC2::VPCGatewayAttachment":
            owner = find(lambda n: True, ["ec2.internet-gateway"])
        elif cfn in {"AWS::RDS::DBSubnetGroup", "AWS::RDS::DBClusterParameterGroup", "AWS::RDS::DBParameterGroup"}:
            owner = find(lambda n: True, ["rds.cluster", "rds.instance"])
        elif cfn.startswith("AWS::ElastiCache::"):
            owner = find(lambda n: True, ["elasticache.replication-group", "elasticache.cluster", "AWS::ElastiCache::ReplicationGroup", "AWS::ElastiCache::CacheCluster"])
        elif cfn.startswith("AWS::EKS::") or cfn == "AWS::EC2::LaunchTemplate":
            # Addon/PodIdentityAssociation ids start with the cluster name; AccessEntry ids end with it (principal|cluster).
            parts = phys.split("|") if "|" in phys else []
            cluster = (parts[-1] if cfn == "AWS::EKS::AccessEntry" else parts[0]) if parts else None
            owner = find(lambda n: cluster is None or any(cluster in r or cluster in n["label"] for r in n["resource_ids"] + [n["label"]]), ["eks.cluster"])
        elif cfn in {"AWS::Logs::LogGroup", "AWS::CloudWatch::Alarm", "AWS::Events::Rule", "AWS::SNS::Subscription", "AWS::Lambda::Permission", "AWS::SQS::QueuePolicy", "AWS::S3::BucketPolicy"}:
            owner = find(lambda n: True, [{"AWS::Logs::LogGroup": "logs.group", "AWS::CloudWatch::Alarm": "cloudwatch.alarm", "AWS::Events::Rule": "events.rule"}.get(cfn, "")])
        if owner is not None:
            attach(owner, key)
    if identities:
        counts = defaultdict(int)
        for _, cfn in identities:
            counts[cfn.split("::")[-1]] += 1
        label = "IAM " + " · ".join(f"{k.lower()}s × {v}" if v > 1 else k.lower() for k, v in sorted(counts.items()))
        nodes.append({"id": f"n{len(nodes) + 1}", "label": label, "detail": "identities declared by the stacks; judged in the IAM findings, not drawn as architecture",
                      "service": "iam", "type": "AWS::IAM::Role", "group": services_group[region], "state": "declared", "resource_ids": [],
                      "stack_resources": [k for k, _ in identities]})


def validate_graph(graph):
    groups = {g["id"]: g for g in graph.get("groups", [])}
    ids = [n["id"] for n in graph["nodes"]]
    edge_ids = [e["id"] for e in graph["edges"]]
    if len(ids) != len(set(ids)) or len(edge_ids) != len(set(edge_ids)) or set(ids) & set(groups):
        raise ValueError("duplicate graph IDs")
    if not ids:
        raise ValueError("no observed nodes; document an empty scope instead of inventing architecture")
    for g in groups.values():
        if g.get("parent") and g["parent"] not in groups:
            raise ValueError(f"group {g['id']} has unknown parent")
    for node in graph["nodes"]:
        if node.get("group") and node["group"] not in groups:
            raise ValueError(f"node {node['id']} is in unknown group")
    for edge in graph["edges"]:
        if edge["source"] not in ids or edge["target"] not in ids:
            raise ValueError("dangling graph edge")


def coverage(graph, inventory, scope):
    """Which required resources, stack resources and placement groups the graph leaves out."""
    ids, stack_keys = required(inventory, scope)
    by_id = {r["id"]: r for r in inventory["resources"]}
    drawn = {rid for n in graph["nodes"] for rid in n.get("resource_ids", [])} | {rid for g in graph.get("groups", []) for rid in g.get("resource_ids", [])}
    drawn_stack = {k for n in graph["nodes"] for k in n.get("stack_resources", [])}
    group_kind = {rid: g.get("kind") for g in graph.get("groups", []) for rid in g.get("resource_ids", [])}
    missing = sorted(ids - drawn)
    not_group = sorted(i for i in ids if by_id[i]["type"] in {"ec2.subnet", "ec2.vpc"}
                       and group_kind.get(i) != ("subnet" if by_id[i]["type"] == "ec2.subnet" else "vpc"))
    azs = {by_id[i]["attributes"].get("AvailabilityZone") for i in ids if by_id[i]["type"] == "ec2.subnet"}
    drawn_azs = {g.get("az") or g["label"].split()[-1] for g in graph.get("groups", []) if g.get("kind") == "az"}
    by_type = defaultdict(int)
    for i in missing:
        by_type[by_id[i]["type"]] += 1
    all_stack = {f"{x['StackName']}/{x['LogicalResourceId']}": x.get("ResourceType") for x in (scope or {}).get("stack_resources", [])}
    physical_drawn = {r["native_id"] for rid in drawn for r in [by_id.get(rid)] if r}
    unaccounted = sorted(k for k, t in all_stack.items() if k not in drawn_stack
                         and not any(x.get("PhysicalResourceId") in physical_drawn for x in scope.get("stack_resources", []) if f"{x['StackName']}/{x['LogicalResourceId']}" == k))
    return {"status": "pass" if not (missing or not_group or (azs - drawn_azs) or (stack_keys - drawn_stack)) else "fail",
            "required_resources": len(ids), "missing_resources": missing, "missing_by_type": dict(by_type),
            "subnets_or_vpcs_not_drawn_as_groups": not_group, "missing_availability_zones": sorted(a for a in azs - drawn_azs if a),
            "required_stack_resources": len(stack_keys), "missing_stack_resources": sorted(stack_keys - drawn_stack),
            "stack_resources_total": len(all_stack), "stack_resources_unaccounted": unaccounted,
            "note": "unaccounted stack resources are informational: every architectural type must be drawn (missing_stack_resources); "
                    "identities and sub-resources are attached to the node they configure and listed here when no node claims them"}


def to_d2(graph):
    validate_graph(graph)
    groups = {g["id"]: g for g in graph.get("groups", [])}

    def path(gid):
        parts = []
        while gid:
            parts.append(gid.replace(".", "_"))
            gid = groups[gid].get("parent")
        return ".".join(reversed(parts))
    lines = ["direction: right", ""]
    for gid in sorted(groups, key=lambda g: path(g).count(".")):
        lines.append(f"{path(gid)}: {json.dumps(groups[gid]['label'], ensure_ascii=False)}")
    node_path = {}
    for node in graph["nodes"]:
        prefix = path(node["group"]) + "." if node.get("group") else ""
        node_path[node["id"]] = prefix + node["id"]
        label = node["label"] + (f"\n{node['detail']}" if node.get("detail") else "")
        lines.append(f"{node_path[node['id']]}: {json.dumps(label, ensure_ascii=False)}")
    for edge in graph["edges"]:
        lines.append(f"{node_path[edge['source']]} -> {node_path[edge['target']]}: {json.dumps(edge.get('label', ''), ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def audit(graph, svg, inventory=None, scope=None, require_coverage=True):
    """Id correspondence between the graph and the final SVG, plus coverage of the inventory when one is given. Without an
    inventory the audit fails unless `require_coverage` is False (a specialist's focused drawing has no inventory to cover)."""
    validate_graph(graph)
    elements = scan_svg(svg.read_text(encoding="utf-8"))
    expected = {"node": {n["id"] for n in graph["nodes"]}, "group": {g["id"] for g in graph.get("groups", [])},
                "edge": {e["id"] for e in graph["edges"]}}
    actual = {kind: [attrs[f"data-{kind}-id"] for _, attrs in elements if f"data-{kind}-id" in attrs] for kind in expected}
    result = {"status": "pass", "visual_inspection": "required", "semantic_inspection": "required",
              "note": "ID correspondence does not prove correct labels, icons, geometry or semantics."}
    for kind, wanted in expected.items():
        result[f"missing_{kind}s"] = sorted(wanted - set(actual[kind]))
        result[f"extra_{kind}s"] = sorted(set(actual[kind]) - wanted)
        if result[f"missing_{kind}s"] or result[f"extra_{kind}s"] or len(actual[kind]) != len(set(actual[kind])):
            result["status"] = "fail"
    if inventory is not None:
        result["coverage"] = coverage(graph, inventory, scope)
        if result["coverage"]["status"] != "pass":
            result["status"] = "fail"
    else:
        result["coverage"] = {"status": "not_checked", "reason": "pass --inventory (and --scope) to check that nothing collected is left out"}
        if require_coverage:
            result["status"] = "fail"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph", type=Path)
    source.add_argument("--audit-graph", type=Path)
    source.add_argument("--from-inventory", action="store_true", help="build the graph from --inventory")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--scope", type=Path, help="scope-resources.json; default: next to the inventory")
    parser.add_argument("--audit-svg", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="as-is")
    parser.add_argument("--title", help="title drawn on the AWS diagram; default: the graph's title or the name")
    parser.add_argument("--note", default="Arrows are configured paths, not measured traffic.", help="one line under the legend")
    parser.add_argument("--icons", type=Path, help="aws-icons folder of the aws-diagram-design skill; default: found beside this skill or in the client's skills folder")
    parser.add_argument("--language", default="en")
    parser.add_argument("--d2", action="store_true", help="also write the D2 source and render it when d2 is installed (structural cross-check)")
    parser.add_argument("--no-draw", action="store_true", help="build and check the graph only")
    argv = sys.argv[1:]
    if "--inventory" in argv and not any(a in argv for a in ("--graph", "--audit-graph", "--from-inventory")):
        argv = ["--from-inventory"] + argv  # backward compatible: --inventory alone builds the graph
    args = parser.parse_args(argv)
    if Path(args.name).name != args.name or args.name in {".", ".."}:
        parser.error("name must be a filename stem")
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        inventory = json.loads(args.inventory.read_text(encoding="utf-8")) if args.inventory else None
        scope = load_scope(args.scope or (args.inventory.parent / "scope-resources.json" if args.inventory else None))
        if args.audit_graph:
            if not args.audit_svg:
                parser.error("--audit-svg is required")
            result = audit(json.loads(args.audit_graph.read_text(encoding="utf-8")), args.audit_svg, inventory, scope)
            (args.output / "diagram-audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: result[k] for k in ("status",)} | {"coverage": result["coverage"]["status"],
                             "missing": len(result["coverage"].get("missing_resources", [])) if isinstance(result["coverage"], dict) else None}))
            return 0 if result["status"] == "pass" else 1
        if args.from_inventory:
            if inventory is None:
                parser.error("--inventory is required")
            graph = graph_from_inventory(inventory, scope)
        else:
            graph = json.loads(args.graph.read_text(encoding="utf-8"))
        validate_graph(graph)
        check = coverage(graph, inventory, scope) if inventory is not None else {"status": "not_checked"}
        (args.output / "diagram-graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = {"coverage": check}
        if args.d2:
            source_text = to_d2(graph)
            source_path = args.output / f"{args.name}.d2"
            svg_path = args.output / f"{args.name}.d2.svg"
            source_path.write_text(source_text, encoding="utf-8")
            if not shutil.which("d2"):
                d2 = {"status": "not_rendered", "reason": "d2_missing", "source": source_path.name}
            else:
                # d2 from PATH (the same resolution which() just performed); both paths are files this run wrote under --output
                process = subprocess.run(["d2", "--layout", "elk", str(source_path), str(svg_path)], capture_output=True, text=True, timeout=120)  # nosec B603 B607
                d2 = {"status": "rendered" if process.returncode == 0 and svg_path.exists() else "failed",
                      "exit_code": process.returncode, "source": source_path.name, "svg": svg_path.name,
                      "source_sha256": hashlib.sha256(source_text.encode()).hexdigest()}
                if d2["status"] == "rendered":
                    scan_svg(svg_path.read_text(encoding="utf-8"))
                    d2["svg_sha256"] = hashlib.sha256(svg_path.read_bytes()).hexdigest()
                else:
                    d2["error"] = process.stderr[-2000:]
            d2["coverage"] = check
            (args.output / "d2-render.json").write_text(json.dumps(d2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["d2"] = d2["status"]
        if check["status"] == "fail":
            print("Graph leaves collected resources out: " + json.dumps({k: check[k] for k in check if k.startswith("missing") and check[k]})[:1500], file=sys.stderr)
            result["status"] = "incomplete"
            print(json.dumps(result | {"coverage": check["status"]}, ensure_ascii=False))
            return 1
        if args.no_draw:
            result["status"] = "graph_only"
            print(json.dumps(result | {"coverage": check["status"]}, ensure_ascii=False))
            return 0
        roots = aws_diagram.icon_roots(args.icons)
        qa = aws_diagram.render(graph, args.output, args.name, args.title, args.note, roots[0] if roots else None, args.language)
        svg_final = args.output / qa["files"]["svg"]
        audit_result = audit(graph, svg_final, inventory, scope, require_coverage=inventory is not None)
        audit_result["geometry"] = {"status": qa["status"], "file": "diagram-qa.json"}
        audit_result["visual_inspection"] = "geometry checked by aws_diagram.py; look at the drawing when a reader will" if qa["status"] == "pass" else "required"
        audit_result["svg_sha256"] = hashlib.sha256(svg_final.read_bytes()).hexdigest()
        (args.output / "diagram-audit.json").write_text(json.dumps(audit_result, indent=2) + "\n", encoding="utf-8")
        result.update({"status": "drawn" if qa["status"] == "pass" and audit_result["status"] == "pass" else "failed", "svg": qa["files"]["svg"], "html": qa["files"]["html"],
                       "icons": qa["checks"]["icons"]["status"], "size": qa["size"], "audit": audit_result["status"], "geometry": qa["status"]})
        print(json.dumps(result | {"coverage": check["status"]}, ensure_ascii=False))
        return 0 if result["status"] == "drawn" else 1
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Diagram stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
