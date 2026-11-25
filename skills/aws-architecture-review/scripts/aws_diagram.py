#!/usr/bin/env python3
"""Draw the AWS-styled architecture diagram from a diagram graph, without an agent redraw.

The layout is the one an architect draws by hand: in a region, the VPCs side by side and the regional services beside
them; in a VPC, the Availability Zones as columns with their subnets stacked (public first), then one band per group of
resources deployed across several subnets, then the VPC-level components (gateways, endpoints, aggregates) in a row.
Nodes are drawn as in the official AWS architecture diagrams: the 48 px service icon with the name under it and one or
two short detail lines, no box (a dashed or coloured outline only for proposed, changed, declared or unconfirmed
components); zones are the official containers (region, VPC, AZ, subnet) with their badges; edges are orthogonal
connectors that end at the icons, with labels beside their own line or numbered markers explained under the legend;
attachments to aggregate nodes (security groups, route tables) are chips on the source node rather than lines. A graph
too large for one readable drawing is drawn as two or three views along its top-level groups (see plan_views). A title
and a legend of states complete the drawing. Every node, group and edge keeps its graph id as data-node-id / data-group-id /
data-edge-id, so `diagram.py --audit-graph` passes on the result. Icons are read from the aws-diagram-design companion
skill (assets/aws-icons) when it is installed beside this skill or in the client's skills folder; without it, nodes carry
a coloured service tag instead of an icon.

Geometry is checked here, not by looking at a PNG: every label fits its box, every node lies inside its group, no two
nodes overlap, every connector ends at its nodes and runs through no other node. The result is diagram-qa.json; a failing check fails the render.

  aws_diagram.py --graph diagram-graph.json --output DIR --name as-is [--title "..."] [--note "..."] [--icons DIR]

Text in the drawing (title, legend, group and node labels) is the English draft unless the graph already carries the
output language; localize_artifacts.py translates SVG text with the rest of the report.
"""
import argparse
import json
from pathlib import Path
import re
import sys

SANS = "'Amazon Ember','Helvetica Neue',Helvetica,Arial,'Apple SD Gothic Neo','Noto Sans KR',sans-serif"
MONO = "'Amazon Ember Mono',ui-monospace,SFMono-Regular,Menlo,monospace"
INK, MUTED, SOFT, ACCENT, RED, PAPER = "#232F3E", "#545B64", "#7D8998", "#EC7211", "#DD344C", "#FFFFFF"

# A node is drawn the way the official AWS architecture diagrams draw it: the 48 px icon, the name centred under it and
# one or two short detail lines, no box. NODE_W is the width of that cell (the text column), which connectors and edge
# labels keep clear of; connectors attach to the icon.
NODE_W = 150
NODE_TOP = 6           # space above the icon inside the cell (room for a state tag)
ICON = 48
TEXT_W = NODE_W - 8
LABEL_SIZE, DETAIL_SIZE, LABEL_LH, DETAIL_LH = 11, 9.5, 13.5, 12
NODE_DETAIL_SIZE, NODE_DETAIL_LH = 9, 11
MAX_LABEL_LINES, MAX_DETAIL_LINES = 3, 2
GROUP_LABEL_SIZE, GROUP_HEAD = 12, 20
TITLE_H = 64
MARGIN = 24

# Zone containers: border colour, dash pattern, group badge file (aws-icons/group/<file>). Official AWS convention.
ZONES = {
    "region": ("#00A4A6", "6,4", "Region_32.svg", "Region"),
    "vpc": ("#8C4FFF", None, "Virtual-private-cloud-VPC_32.svg", "VPC"),
    "az": ("#00A4A6", "6,4", None, "Availability Zone"),
    "subnet": ("#00A4A6", None, "Private-subnet_32.svg", "Private subnet"),
    "public-subnet": ("#7AA116", None, "Public-subnet_32.svg", "Public subnet"),
    "subnet-span": ("#00A4A6", "2,4", None, "Resources spanning several subnets"),
    "services": ("#7D8998", "6,4", None, "Regional services outside the VPC"),
    "account": ("#E7157B", None, "AWS-Account_32.svg", "AWS account"),
    "cloud": ("#232F3E", None, "AWS-Cloud_32.svg", "AWS Cloud"),
    "security-group": ("#DD344C", "4,4", None, "Security group"),
    "autoscaling": ("#ED7100", "4,4", "Auto-Scaling-group_32.svg", "Auto Scaling group"),
    "external": ("#7D8998", "2,3", None, "Components drawn in full in another view"),
    "other": ("#545B64", None, None, "Group"),
}
# Node states: stroke colour, dash pattern, stroke width, legend text. Observed and retained are the plain box.
STATES = {
    "observed": (INK, None, 1, "Observed in the account (configuration read by API)"),
    "retained": (INK, None, 1, "Retained as it is"),
    "declared": (MUTED, "3,3", 1, "Declared in a template or source; not observed as a resource"),
    "changed": (ACCENT, None, 1.6, "Existing component the proposal changes"),
    "proposed": (ACCENT, "5,3", 1.6, "Proposed; does not exist yet"),
    "removal_candidate": (RED, "5,3", 1.6, "Candidate for removal"),
    "unresolved": (SOFT, "3,3", 1, "Could not be confirmed from the evidence"),
    "elsewhere": (SOFT, "2,3", 1, "Drawn in full in another view; shown here for its connections"),
}
PLAIN_STATES = {"observed", "retained"}  # drawn without an outline, as in the official diagrams
# Edges to these aggregate nodes (every resource is attached to some security group and route table) are drawn as a chip
# on the source node instead of a routed line, which is what an architect does by hand; the edge keeps its id on the chip.
BADGE_TARGETS = {"ec2.security-group", "ec2.route-table", "elbv2.target-group:aggregate"}
EDGE_STATES = {
    "observed_configuration": (MUTED, None, "Configured path (not measured traffic)"),
    "observed": (MUTED, None, "Configured path (not measured traffic)"),
    "declared": (MUTED, "4,3", "Declared in a template or source"),
    "proposed": (ACCENT, "5,3", "Proposed path"),
    "unresolved": (SOFT, "2,3", "Unconfirmed path"),
}

# Node type -> (icon path under aws-icons/, category colour, short tag). Types are collect.py's `service.kind` and
# CloudFormation `AWS::Service::Type`. Unknown types fall back to the service prefix, then to a grey tag.
ICONS = {
    "ec2.instance": ("resource/Compute/Res_Amazon-EC2_Instance_48.svg", "#ED7100", "EC2"),
    "ec2": ("service/Compute/Arch_Amazon-EC2_48.svg", "#ED7100", "EC2"),
    "ec2.nat-gateway": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_NAT-Gateway_48.svg", "#8C4FFF", "NAT"),
    "ec2.internet-gateway": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_Internet-Gateway_48.svg", "#8C4FFF", "IGW"),
    "ec2.vpc-endpoint": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_Endpoints_48.svg", "#8C4FFF", "VPCE"),
    "ec2.elastic-ip": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_Elastic-Network-Adapter_48.svg", "#8C4FFF", "EIP"),
    "ec2.route-table": ("resource/Networking-Content-Delivery/Res_Amazon-Route-53_Route-Table_48.svg", "#8C4FFF", "RT"),
    "ec2.security-group": ("resource/General-Icons/Res_Shield_48_Light.svg", "#DD344C", "SG"),
    "ec2.flow-log": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_Flow-Logs_48.svg", "#8C4FFF", "Flow"),
    "ec2.volume": ("resource/Storage/Res_Amazon-Elastic-Block-Store_Volume_48.svg", "#7AA116", "EBS"),
    "ec2.vpn-gateway": ("resource/Networking-Content-Delivery/Res_Amazon-VPC_VPN-Gateway_48.svg", "#8C4FFF", "VPN"),
    "ec2.transit-gateway": ("service/Networking-Content-Delivery/Arch_AWS-Transit-Gateway_48.svg", "#8C4FFF", "TGW"),
    "autoscaling.group": ("resource/Compute/Res_Amazon-EC2_Auto-Scaling_48.svg", "#ED7100", "ASG"),
    "ecs.cluster": ("service/Containers/Arch_Amazon-Elastic-Container-Service_48.svg", "#ED7100", "ECS"),
    "ecs.service": ("resource/Containers/Res_Amazon-Elastic-Container-Service_Service_48.svg", "#ED7100", "ECS"),
    "ecs.task-definition": ("resource/Containers/Res_Amazon-Elastic-Container-Service_Task_48.svg", "#ED7100", "Task"),
    "ecs": ("service/Containers/Arch_Amazon-Elastic-Container-Service_48.svg", "#ED7100", "ECS"),
    "eks.cluster": ("service/Containers/Arch_Amazon-Elastic-Kubernetes-Service_48.svg", "#ED7100", "EKS"),
    "eks.nodegroup": ("resource/Compute/Res_Amazon-EC2_Instances_48.svg", "#ED7100", "Nodes"),
    "eks": ("service/Containers/Arch_Amazon-Elastic-Kubernetes-Service_48.svg", "#ED7100", "EKS"),
    "elbv2.ip-targets": ("resource/Containers/Res_Amazon-Elastic-Kubernetes-Service_EKS-on-Outposts_48.svg", "#ED7100", "Pods"),
    "ecr.repository": ("service/Containers/Arch_Amazon-Elastic-Container-Registry_48.svg", "#ED7100", "ECR"),
    "lambda.function": ("resource/Compute/Res_AWS-Lambda_Lambda-Function_48.svg", "#ED7100", "Fn"),
    "lambda": ("service/Compute/Arch_AWS-Lambda_48.svg", "#ED7100", "Lambda"),
    "elbv2.load-balancer": ("resource/Networking-Content-Delivery/Res_Elastic-Load-Balancing_Application-Load-Balancer_48.svg", "#8C4FFF", "ALB"),
    "elbv2.target-group": ("resource/General-Icons/Res_Servers_48_Light.svg", "#8C4FFF", "TG"),
    "elbv2": ("service/Networking-Content-Delivery/Arch_Elastic-Load-Balancing_48.svg", "#8C4FFF", "ELB"),
    "rds.db-instance": ("resource/Databases/Res_Amazon-Aurora-Instance_48.svg", "#C925D1", "DB"),
    "rds.instance": ("resource/Databases/Res_Amazon-Aurora-Instance_48.svg", "#C925D1", "DB"),
    "rds.cluster": ("service/Databases/Arch_Amazon-Aurora_48.svg", "#C925D1", "Aurora"),
    "rds.db-cluster": ("service/Databases/Arch_Amazon-Aurora_48.svg", "#C925D1", "Aurora"),
    "rds": ("service/Databases/Arch_Amazon-RDS_48.svg", "#C925D1", "RDS"),
    "elasticache.replication-group": ("service/Databases/Arch_Amazon-ElastiCache_48.svg", "#C925D1", "Cache"),
    "elasticache.cluster": ("service/Databases/Arch_Amazon-ElastiCache_48.svg", "#C925D1", "Cache"),
    "elasticache": ("service/Databases/Arch_Amazon-ElastiCache_48.svg", "#C925D1", "Cache"),
    "dynamodb.table": ("resource/Databases/Res_Amazon-DynamoDB_Table_48.svg", "#C925D1", "DDB"),
    "dynamodb": ("service/Databases/Arch_Amazon-DynamoDB_48.svg", "#C925D1", "DDB"),
    "opensearch.domain": ("service/Analytics/Arch_Amazon-OpenSearch-Service_48.svg", "#8C4FFF", "OS"),
    "s3.bucket": ("resource/Storage/Res_Amazon-Simple-Storage-Service_Bucket_48.svg", "#7AA116", "S3"),
    "s3": ("service/Storage/Arch_Amazon-Simple-Storage-Service_48.svg", "#7AA116", "S3"),
    "efs.file-system": ("service/Storage/Arch_Amazon-EFS_48.svg", "#7AA116", "EFS"),
    "backup.plan": ("service/Storage/Arch_AWS-Backup_48.svg", "#7AA116", "Backup"),
    "backup.vault": ("resource/Storage/Res_AWS-Backup_Backup-Vault_48.svg", "#7AA116", "Vault"),
    "codepipeline.pipeline": ("service/Developer-Tools/Arch_AWS-CodePipeline_48.svg", "#C925D1", "Pipe"),
    "codebuild.project": ("service/Developer-Tools/Arch_AWS-CodeBuild_48.svg", "#C925D1", "Build"),
    "codecommit.repository": ("service/Developer-Tools/Arch_AWS-CodeCommit_48.svg", "#C925D1", "Repo"),
    "codedeploy.application": ("service/Developer-Tools/Arch_AWS-CodeDeploy_48.svg", "#C925D1", "Deploy"),
    "sns.topic": ("resource/Application-Integration/Res_Amazon-Simple-Notification-Service_Topic_48.svg", "#E7157B", "SNS"),
    "sqs.queue": ("resource/Application-Integration/Res_Amazon-Simple-Queue-Service_Queue_48.svg", "#E7157B", "SQS"),
    "events.rule": ("resource/Application-Integration/Res_Amazon-EventBridge_Rule_48.svg", "#E7157B", "Rule"),
    "stepfunctions.state-machine": ("service/Application-Integration/Arch_AWS-Step-Functions_48.svg", "#E7157B", "SFN"),
    "cloudwatch.alarm": ("resource/Management-Governance/Res_Amazon-CloudWatch_Alarm_48.svg", "#E7157B", "Alarm"),
    "cloudwatch.dashboard": ("service/Management-Tools/Arch_Amazon-CloudWatch_48.svg", "#E7157B", "Dash"),
    "logs.group": ("resource/Management-Governance/Res_Amazon-CloudWatch_Logs_48.svg", "#E7157B", "Logs"),
    "secretsmanager.secret": ("service/Security-Identity/Arch_AWS-Secrets-Manager_48.svg", "#DD344C", "Secret"),
    "kms.alias": ("service/Security-Identity/Arch_AWS-Key-Management-Service_48.svg", "#DD344C", "KMS"),
    "kms.key": ("service/Security-Identity/Arch_AWS-Key-Management-Service_48.svg", "#DD344C", "KMS"),
    "acm.certificate": ("service/Security-Identity/Arch_AWS-Certificate-Manager_48.svg", "#DD344C", "ACM"),
    "wafv2.web-acl": ("service/Security-Identity/Arch_AWS-WAF_48.svg", "#DD344C", "WAF"),
    "cognito.user-pool": ("service/Security-Identity/Arch_Amazon-Cognito_48.svg", "#DD344C", "Cognito"),
    "iam.role": ("resource/Security-Identity/Res_AWS-Identity-Access-Management_Role_48.svg", "#DD344C", "Role"),
    "iam": ("service/Security-Identity/Arch_AWS-Identity-and-Access-Management_48.svg", "#DD344C", "IAM"),
    "secretsmanager": ("service/Security-Identity/Arch_AWS-Secrets-Manager_48.svg", "#DD344C", "Secret"),
    "inspector": ("service/Security-Identity/Arch_Amazon-Inspector_48.svg", "#DD344C", "Insp"),
    "codebuild": ("service/Developer-Tools/Arch_AWS-CodeBuild_48.svg", "#C925D1", "Build"),
    "codecommit": ("service/Developer-Tools/Arch_AWS-CodeCommit_48.svg", "#C925D1", "Repo"),
    "codepipeline": ("service/Developer-Tools/Arch_AWS-CodePipeline_48.svg", "#C925D1", "Pipe"),
    "ecr": ("service/Containers/Arch_Amazon-Elastic-Container-Registry_48.svg", "#ED7100", "ECR"),
    "events": ("service/Application-Integration/Arch_Amazon-EventBridge_48.svg", "#E7157B", "Events"),
    "sns": ("service/Application-Integration/Arch_Amazon-Simple-Notification-Service_48.svg", "#E7157B", "SNS"),
    "sqs": ("service/Application-Integration/Arch_Amazon-Simple-Queue-Service_48.svg", "#E7157B", "SQS"),
    "cloudwatch": ("service/Management-Tools/Arch_Amazon-CloudWatch_48.svg", "#E7157B", "CW"),
    "cloudwatch.logs": ("resource/Management-Governance/Res_Amazon-CloudWatch_Logs_48.svg", "#E7157B", "Logs"),
    "logs": ("resource/Management-Governance/Res_Amazon-CloudWatch_Logs_48.svg", "#E7157B", "Logs"),
    "athena": ("service/Analytics/Arch_Amazon-Athena_48.svg", "#8C4FFF", "Athena"),
    "redshift": ("service/Analytics/Arch_Amazon-Redshift_48.svg", "#8C4FFF", "Redshift"),
    "glue": ("service/Analytics/Arch_AWS-Glue_48.svg", "#8C4FFF", "Glue"),
    "ssm": ("service/Management-Tools/Arch_AWS-Systems-Manager_48.svg", "#E7157B", "SSM"),
    "client": ("resource/General-Icons/Res_Client_48_Light.svg", "#232F3E", "Client"),
    "external.client": ("resource/General-Icons/Res_Client_48_Light.svg", "#232F3E", "Client"),
    "general": ("resource/General-Icons/Res_Generic-Application_48_Light.svg", "#232F3E", "App"),
    "unknown": ("resource/General-Icons/Res_Generic-Application_48_Light.svg", "#7D8998", "?"),
    "apigateway.rest-api": ("service/Networking-Content-Delivery/Arch_Amazon-API-Gateway_48.svg", "#8C4FFF", "API"),
    "apigatewayv2.api": ("service/Networking-Content-Delivery/Arch_Amazon-API-Gateway_48.svg", "#8C4FFF", "API"),
    "cloudfront.distribution": ("service/Networking-Content-Delivery/Arch_Amazon-CloudFront_48.svg", "#8C4FFF", "CDN"),
    "route53.hosted-zone": ("service/Networking-Content-Delivery/Arch_Amazon-Route-53_48.svg", "#8C4FFF", "R53"),
    "kinesis.stream": ("service/Analytics/Arch_Amazon-Kinesis-Data-Streams_48.svg", "#8C4FFF", "Kinesis"),
    "firehose.delivery-stream": ("service/Analytics/Arch_Amazon-Data-Firehose_48.svg", "#8C4FFF", "Firehose"),
    "kafka.cluster": ("service/Analytics/Arch_Amazon-Managed-Streaming-for-Apache-Kafka_48.svg", "#8C4FFF", "MSK"),
    "glue.database": ("service/Analytics/Arch_AWS-Glue_48.svg", "#8C4FFF", "Glue"),
    "glue.job": ("service/Analytics/Arch_AWS-Glue_48.svg", "#8C4FFF", "Glue"),
    "athena.workgroup": ("service/Analytics/Arch_Amazon-Athena_48.svg", "#8C4FFF", "Athena"),
    "redshift.cluster": ("service/Analytics/Arch_Amazon-Redshift_48.svg", "#8C4FFF", "Redshift"),
    "bedrock.agent": ("service/Artificial-Intelligence/Arch_Amazon-Bedrock_48.svg", "#01A88D", "Bedrock"),
    "bedrock.knowledge-base": ("service/Artificial-Intelligence/Arch_Amazon-Bedrock_48.svg", "#01A88D", "KB"),
    "bedrock": ("service/Artificial-Intelligence/Arch_Amazon-Bedrock_48.svg", "#01A88D", "Bedrock"),
    "sagemaker.endpoint": ("service/Artificial-Intelligence/Arch_Amazon-SageMaker-AI_48.svg", "#01A88D", "SM"),
    "iot.thing": ("service/Internet-of-Things/Arch_AWS-IoT-Core_48.svg", "#7AA116", "IoT"),
    "ssm.parameter": ("service/Management-Tools/Arch_AWS-Systems-Manager_48.svg", "#E7157B", "SSM"),
    "internet": ("resource/General-Icons/Res_Internet_48_Light.svg", INK, "Net"),
    "users": ("resource/General-Icons/Res_Users_48_Light.svg", INK, "Users"),
}
CFN_TO_TYPE = {
    "AWS::ElastiCache::ReplicationGroup": "elasticache.replication-group", "AWS::ElastiCache::CacheCluster": "elasticache.cluster",
    "AWS::ElastiCache::ServerlessCache": "elasticache", "AWS::ECR::Repository": "ecr.repository", "AWS::CodePipeline::Pipeline": "codepipeline.pipeline",
    "AWS::CodeBuild::Project": "codebuild.project", "AWS::CodeCommit::Repository": "codecommit.repository", "AWS::SecretsManager::Secret": "secretsmanager.secret",
    "AWS::EKS::Nodegroup": "eks.nodegroup", "AWS::SQS::Queue": "sqs.queue", "AWS::Lambda::Function": "lambda.function", "AWS::DynamoDB::Table": "dynamodb.table",
    "AWS::Kinesis::Stream": "kinesis.stream", "AWS::StepFunctions::StateMachine": "stepfunctions.state-machine", "AWS::ApiGateway::RestApi": "apigateway.rest-api",
    "AWS::ApiGatewayV2::Api": "apigatewayv2.api", "AWS::CloudFront::Distribution": "cloudfront.distribution", "AWS::EFS::FileSystem": "efs.file-system",
    "AWS::OpenSearchService::Domain": "opensearch.domain", "AWS::MSK::Cluster": "kafka.cluster", "AWS::Cognito::UserPool": "cognito.user-pool",
    "AWS::CloudWatch::Dashboard": "cloudwatch.dashboard", "AWS::Bedrock::Agent": "bedrock.agent", "AWS::Bedrock::KnowledgeBase": "bedrock.knowledge-base",
    "AWS::AutoScaling::AutoScalingGroup": "autoscaling.group", "AWS::ECS::Service": "ecs.service", "AWS::ECS::Cluster": "ecs.cluster",
    "AWS::EKS::Cluster": "eks.cluster", "AWS::RDS::DBCluster": "rds.db-cluster", "AWS::RDS::DBInstance": "rds.db-instance", "AWS::S3::Bucket": "s3.bucket",
    "AWS::SNS::Topic": "sns.topic", "AWS::ElasticLoadBalancingV2::LoadBalancer": "elbv2.load-balancer", "AWS::EC2::Instance": "ec2.instance",
}


# ---------------------------------------------------------------------------
# Text measurement and wrapping (no font available at render time; widths are estimates for Ember/Helvetica metrics)
# ---------------------------------------------------------------------------
def char_width(ch, size):
    o = ord(ch)
    if 0x1100 <= o <= 0x11FF or 0x3000 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
        return size * 1.0
    if ch in "iljtfr.,:;|!'’`":
        return size * 0.32
    if ch == " ":
        return size * 0.28
    if ch.isdigit():
        return size * 0.58
    if ch.isupper():
        return size * 0.70 if ch in "MW" else size * 0.64
    if ch.islower():
        return size * 0.60 if ch in "mw" else size * 0.54
    return size * 0.5


def text_width(text, size):
    return sum(char_width(c, size) for c in text) * 1.03


def wrap(text, size, width, max_lines):
    """Lines that fit `width`, breaking at spaces and after separators; a word longer than the line is cut by characters.
    Returns (lines, truncated)."""
    text = " ".join(str(text or "").split())
    if not text:
        return [], False
    tokens = re.findall(r"[^\s·/\-_,]+[·/\-_,]*\s*|\s+", text)
    lines, current = [], ""
    for token in tokens:
        piece = token
        if text_width(current + piece, size) <= width or not current.strip():
            if text_width(current + piece, size) <= width:
                current += piece
                continue
            # a single token wider than the line: cut it
            while piece:
                cut = len(piece)
                while cut > 1 and text_width(current + piece[:cut], size) > width:
                    cut -= 1
                current += piece[:cut]
                piece = piece[cut:]
                if piece:
                    lines.append(current.rstrip()); current = ""
            continue
        lines.append(current.rstrip()); current = piece.lstrip() if not piece.strip() else piece
    if current.strip():
        lines.append(current.rstrip())
    truncated = False
    if len(lines) > max_lines:
        lines, truncated = lines[:max_lines], True
        last = lines[-1]
        while last and text_width(last + "…", size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines, truncated


# ---------------------------------------------------------------------------
# Icons from the aws-diagram-design companion skill
# ---------------------------------------------------------------------------
def icon_roots(explicit=None):
    """Where the official icons are: an explicit --icons folder (used as given, no fallback), $MODA_AWS_ICONS, the
    aws-diagram-design skill beside this one, or the client's skills folders under the project and the home directory."""
    here = Path(__file__).resolve()
    if explicit is not None:
        return [explicit] if (explicit / "INDEX.md").is_file() else []
    candidates = []
    env = __import__("os").environ.get("MODA_AWS_ICONS")
    if env:
        candidates.append(Path(env))
    candidates.append(here.parents[2] / "aws-diagram-design" / "assets" / "aws-icons")  # skills root sibling
    for base in (Path.cwd(), Path.home()):
        for client in (".kiro", ".claude", ".agents"):
            candidates.append(base / client / "skills" / "aws-diagram-design" / "assets" / "aws-icons")
    return [c for c in candidates if c and (c / "INDEX.md").is_file()]


class IconSet:
    def __init__(self, root):
        self.root = root
        self.cache = {}

    def inner(self, relative):
        """The icon's drawing as a group scaled to 1 px per viewBox unit, or None. The file's outer <svg>, <title> and ids
        are dropped (two icons of one category share a background id)."""
        if self.root is None:
            return None
        if relative in self.cache:
            return self.cache[relative]
        path = self.root / relative
        result = None
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            box = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', text)
            body = re.sub(r"^.*?<svg[^>]*>", "", text, count=1, flags=re.S)
            body = re.sub(r"</svg>\s*$", "", body, flags=re.S)
            body = re.sub(r"<title>.*?</title>", "", body, flags=re.S)
            body = re.sub(r"<\?xml[^>]*\?>|<!--.*?-->", "", body, flags=re.S)
            body = re.sub(r'\sid="[^"]*"', "", body)
            if box and "<script" not in body.lower():
                result = (body.strip(), float(box.group(3)), float(box.group(4)))
        self.cache[relative] = result
        return result


def icon_for(node):
    kind = node.get("type") or ""
    kind = CFN_TO_TYPE.get(kind, kind)
    if kind in ICONS:
        return ICONS[kind]
    prefix = kind.split(".")[0].split("::")[0].lower()
    if prefix in ICONS:
        return ICONS[prefix]
    service = str(node.get("service") or prefix or "aws").lower()
    if service in ICONS:
        return ICONS[service]
    return (None, SOFT, service[:6].upper() or "AWS")


# ---------------------------------------------------------------------------
# Layout: the arrangement an architect draws by hand
# ---------------------------------------------------------------------------
# Region: VPCs side by side, then the regional-services group, then loose nodes. VPC: Availability Zones as columns
# (subnets stacked inside, public first), then one band per subnet span (resources deployed in several subnets), then
# the VPC-level nodes (gateways, endpoints, aggregates) in a row. Other groups: a grid. Sizes are computed bottom-up,
# positions top-down; connectors are orthogonal with ports spread along each side.
def esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def node_lines(node, width=TEXT_W):
    label, t1 = wrap(node.get("label", ""), LABEL_SIZE, width, MAX_LABEL_LINES)
    detail, t2 = wrap(node.get("detail", ""), NODE_DETAIL_SIZE, width, MAX_DETAIL_LINES)
    return label, detail, t1 or t2


BADGE_ROW = 18
GAP = 40           # between siblings, vertically; leaves a corridor for two or three connectors
GAP_X = 96         # between siblings, horizontally: room for an arrow and a short label between two cells
MAX_ROW_W = 1400   # a row of groups wraps beyond this width, so a drawing stays readable at page width
PAD = 16           # inside a group, left/right/bottom
HEAD = 40          # inside a group, top: the title band (24) and a free row a connector can turn in before it leaves the group


def node_size(node, badges=0):
    label, detail, _ = node_lines(node)
    text_h = len(label) * LABEL_LH + (len(detail) * NODE_DETAIL_LH + 3 if detail else 0)
    return NODE_W, NODE_TOP + ICON + 6 + text_h + 4 + (BADGE_ROW if badges else 0)


def icon_box(box):
    """The icon inside a node cell: centred, at the top."""
    x, y, w, h = box
    return (x + (w - ICON) / 2, y + NODE_TOP, ICON, ICON)


def is_badge_edge(edge, nodes):
    target = nodes.get(edge["target"]) or {}
    hint = str(edge.get("drawn_as") or "").lower()
    return "badge" in hint or target.get("type") in BADGE_TARGETS


def subnet_order(group):
    return (0 if is_public_subnet(group) else 1, str(group.get("label", "")).lower())


def grid(items, columns):
    """Place (id, w, h) items in rows of `columns`; returns ({id: (x, y)}, w, h) relative to the content origin."""
    pos, x, y, row_h, width = {}, 0, 0, 0, 0
    for i, (iid, w, h) in enumerate(items):
        if i and i % columns == 0:
            x, y, row_h = 0, y + row_h + GAP, 0
        pos[iid] = (x, y)
        x += w + GAP_X
        row_h = max(row_h, h)
        width = max(width, x - GAP_X)
    return pos, width, (y + row_h) if items else 0


def rows(items, max_width=MAX_ROW_W):
    """Place (id, w, h) items left to right, starting a new row when the next one would pass `max_width`."""
    pos, x, y, row_h, width = {}, 0, 0, 0, 0
    for iid, w, h in items:
        if x and x + w > max_width:
            x, y, row_h = 0, y + row_h + GAP, 0
        pos[iid] = (x, y)
        x += w + GAP_X
        row_h = max(row_h, h)
        width = max(width, x - GAP_X)
    return pos, width, (y + row_h) if items else 0


def columns_for(n, kind):
    if kind == "az":
        return 1
    if kind == "subnet":
        return min(n, 2) if n <= 4 else 3
    if kind == "subnet-span":
        return min(n, 5) or 1
    if n <= 4:
        return max(1, n)
    return min(5, -(-n // 2)) if n <= 10 else 5


GATEWAYS = {"ec2.internet-gateway", "ec2.vpn-gateway", "ec2.egress-only-internet-gateway", "ec2.transit-gateway-attachment"}


def flow_order(ids, edges):
    """Members of one group in the order their connectors run (a chain of sources and targets kept next to each other),
    so the grid puts connected components side by side instead of in graph order. Unconnected members follow."""
    inside = set(ids)
    out, incoming = {}, {i: 0 for i in ids}
    for e in edges:
        if e["source"] in inside and e["target"] in inside and e["source"] != e["target"]:
            out.setdefault(e["source"], []).append(e["target"]); incoming[e["target"]] += 1
    linked = {i for i in ids if out.get(i) or incoming[i]}
    order, seen = [], set()

    def visit(i):
        if i in seen:
            return
        seen.add(i); order.append(i)
        for j in out.get(i, []):
            visit(j)
    for i in [i for i in ids if i in linked and not incoming[i]] + [i for i in ids if i in linked]:
        visit(i)
    return order + [i for i in ids if i not in seen]


class Layout:
    def __init__(self, graph):
        self.graph = graph
        self.groups = {g["id"]: g for g in graph.get("groups", [])}
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.children = {gid: [] for gid in self.groups}      # child group ids
        self.members = {gid: [] for gid in self.groups}       # node ids directly in the group
        self.roots = []
        for gid, g in self.groups.items():
            (self.children[g["parent"]] if g.get("parent") in self.groups else self.roots).append(gid)
        self.loose = []
        for n in graph["nodes"]:
            (self.members[n["group"]] if n.get("group") in self.groups else self.loose).append(n["id"])
        self.badges = {}
        for e in graph["edges"]:
            if is_badge_edge(e, self.nodes):
                self.badges[e["source"]] = self.badges.get(e["source"], 0) + 1
        self.size = {}      # id -> (w, h)
        self.rel = {}       # id -> (x, y) relative to the parent's content origin
        self.boxes = {}     # id -> (x, y, w, h) absolute

    def measure_node(self, nid):
        self.size[nid] = node_size(self.nodes[nid], self.badges.get(nid, 0))
        return self.size[nid]

    def measure_group(self, gid):
        g = self.groups[gid]
        kind = g.get("kind") or "other"
        for cid in self.children[gid]:
            self.measure_group(cid)
        for nid in self.members[gid]:
            self.measure_node(nid)
        label_w = text_width(str(g.get("label", "")), GROUP_LABEL_SIZE) + 44
        if kind == "vpc":
            w, h = self.arrange_vpc(gid)
        elif kind == "region":
            w, h = self.arrange_region(gid)
        else:
            kids = sorted(self.children[gid], key=lambda c: subnet_order(self.groups[c])) if kind == "az" else list(self.children[gid])
            members = flow_order(self.members[gid], self.graph["edges"])
            items = [(cid, *self.size[cid]) for cid in kids] + [(nid, *self.size[nid]) for nid in members]
            cols = 1 if kind == "az" else columns_for(len(items), kind)
            pos, w, h = grid(items, cols)
            self.rel.update(pos)
        w = max(w, label_w, 120)
        self.size[gid] = (w + 2 * PAD, h + HEAD + PAD)
        return self.size[gid]

    def arrange_region(self, gid):
        kids = self.children[gid]
        vpcs = [c for c in kids if (self.groups[c].get("kind") or "") == "vpc"]
        services = [c for c in kids if (self.groups[c].get("kind") or "") == "services"]
        rest = [c for c in kids if c not in vpcs and c not in services]
        order = vpcs + services + rest
        pos, w, h = rows([(cid, *self.size[cid]) for cid in order], max(MAX_ROW_W, max([self.size[c][0] for c in order] or [0])))
        self.rel.update(pos)
        if self.members[gid]:
            items = [(nid, *self.size[nid]) for nid in self.members[gid]]
            pos, pw, ph = grid(items, 1 if order else columns_for(len(items), "other"))
            for nid, (px, py) in pos.items():
                self.rel[nid] = (px + (w + GAP_X if order else 0), py)
            w = w + GAP_X + pw if order else pw
            h = max(h, ph)
        return w, h

    def arrange_vpc(self, gid):
        kids = self.children[gid]
        azs = sorted([c for c in kids if (self.groups[c].get("kind") or "") == "az"], key=lambda c: str(self.groups[c].get("az") or self.groups[c].get("label")))
        spans = [c for c in kids if (self.groups[c].get("kind") or "") == "subnet-span"]
        others = [c for c in kids if c not in azs and c not in spans]
        gates = [n for n in self.members[gid] if (self.nodes[n].get("type") or "") in GATEWAYS]
        y0 = 0
        if gates:  # the internet and VPN gateways sit on top, next to the public subnets, as in the official diagrams
            pos, gw, gh = rows([(nid, *self.size[nid]) for nid in gates])
            self.rel.update(pos); y0 = gh + GAP
        top = [(cid, *self.size[cid]) for cid in azs + others]
        pos, width, row_h = rows(top, max(MAX_ROW_W, sum(self.size[c][0] + GAP_X for c in azs)))  # the AZ columns stay side by side
        self.rel.update({k: (x, y + y0) for k, (x, y) in pos.items()})
        row_h += y0
        width = max(width, gw if gates else 0)
        y = row_h + (GAP if row_h else 0)
        for cid in spans:
            cw, ch = self.size[cid]
            self.rel[cid] = (0, y); y += ch + GAP; width = max(width, cw)
        rest = [n for n in flow_order(self.members[gid], self.graph["edges"]) if n not in gates]
        if rest:
            items = [(nid, *self.size[nid]) for nid in rest]
            cols = max(1, min(len(items), (max(width, NODE_W) + GAP_X) // (NODE_W + GAP_X))) if width else columns_for(len(items), "other")
            pos, pw, ph = grid(items, cols)
            for nid, (px, py) in pos.items():
                self.rel[nid] = (px, py + y)
            width = max(width, pw); y += ph + GAP
        # widen span bands to the VPC content width so they read as bands across the AZ columns
        for cid in spans:
            cw, ch = self.size[cid]
            self.size[cid] = (max(cw, width), ch)
        return width, max(y - GAP, 0)

    def place(self, iid, ox, oy):
        x, y = self.rel.get(iid, (0, 0))
        w, h = self.size[iid]
        ax, ay = ox + x, oy + y
        self.boxes[iid] = (ax, ay, w, h)
        if iid in self.groups:
            for cid in self.children[iid]:
                self.place(cid, ax + PAD, ay + HEAD)
            for nid in self.members[iid]:
                self.place(nid, ax + PAD, ay + HEAD)

    def run(self):
        for gid in self.roots:
            self.measure_group(gid)
        for nid in self.loose:
            self.measure_node(nid)
        items = [(gid, *self.size[gid]) for gid in self.roots] + [(nid, *self.size[nid]) for nid in self.loose]
        pos, w, h = rows(items, max(MAX_ROW_W, max([i[1] for i in items] or [0]))) if self.roots else grid(items, columns_for(len(items), "other"))
        self.rel.update(pos)
        for iid in [i for i, _, _ in items]:
            self.place(iid, 0, 0)
        return self.boxes


def group_headers(graph, boxes):
    """The title band of each group (badge and label), which connectors and edge labels keep clear of."""
    out = []
    for g in graph.get("groups", []):
        if g["id"] not in boxes:
            continue
        x, y, w, h = boxes[g["id"]]
        width = min(w, 34 + text_width(str(g.get("label") or ""), GROUP_LABEL_SIZE) + 10)
        out.append((x, y, width, 24))
    return out


def route_edges(graph, boxes, badge_ids, cell=8):
    """Orthogonal connectors that go around nodes. Each edge leaves its source on the side facing the target (ports
    spread along the side), then an A* search on a grid finds the shortest path that avoids node boxes, with a penalty
    for every turn and for cells other connectors already use. Returns {edge id: (points, label point)}."""
    import heapq
    if not boxes:
        return {}
    min_x = min(b[0] for b in boxes.values()) - 3 * cell; min_y = min(b[1] for b in boxes.values()) - 3 * cell
    max_x = max(b[0] + b[2] for b in boxes.values()) + 3 * cell; max_y = max(b[1] + b[3] for b in boxes.values()) + 3 * cell
    cols, rows = int((max_x - min_x) // cell) + 1, int((max_y - min_y) // cell) + 1
    node_ids = {n["id"] for n in graph["nodes"]}

    def to_cell(x, y):
        return (min(cols - 1, max(0, int((x - min_x) // cell))), min(rows - 1, max(0, int((y - min_y) // cell))))

    def centre(c):
        return (min_x + (c[0] + 0.5) * cell, min_y + (c[1] + 0.5) * cell)
    blocked = set()
    node_boxes = [boxes[nid] for nid in node_ids]
    inflate = 3
    for nid in node_ids:
        x, y, w, h = boxes[nid]
        c0, c1 = to_cell(x - inflate, y - inflate), to_cell(x + w + inflate, y + h + inflate)
        for cx in range(c0[0], c1[0] + 1):
            for cy in range(c0[1], c1[1] + 1):
                blocked.add((cx, cy))
    used = {}
    toll = {}  # cells that cost extra: group borders (a connector should not run along one) and group titles (crossed only when there is no other way)
    for g in graph.get("groups", []):
        if g["id"] not in boxes:
            continue
        x, y, w, h = boxes[g["id"]]
        c0, c1 = to_cell(x, y), to_cell(x + w, y + h)
        for cx in range(c0[0], c1[0] + 1):
            for cy in (c0[1], c1[1]):
                toll[(cx, cy)] = max(toll.get((cx, cy), 0), 4)
        for cy in range(c0[1], c1[1] + 1):
            for cx in (c0[0], c1[0]):
                toll[(cx, cy)] = max(toll.get((cx, cy), 0), 4)
    for x, y, w, h in group_headers(graph, boxes):
        c0, c1 = to_cell(x, y), to_cell(x + w, y + h)
        for cx in range(c0[0], c1[0] + 1):
            for cy in range(c0[1], c1[1] + 1):
                toll[(cx, cy)] = 400
    plan = {}
    for e in graph["edges"]:
        if e["id"] in badge_ids or e["source"] not in boxes or e["target"] not in boxes:
            continue
        sx, sy, sw, sh = boxes[e["source"]]; tx, ty, tw, th = boxes[e["target"]]
        scx, scy, tcx, tcy = sx + sw / 2, sy + sh / 2, tx + tw / 2, ty + th / 2
        if abs(tcx - scx) * 0.8 >= abs(tcy - scy) or (tx >= sx + sw or tx + tw <= sx):
            plan[e["id"]] = ("right", "left") if tcx >= scx else ("left", "right")
        else:
            plan[e["id"]] = ("bottom", "top") if tcy >= scy else ("top", "bottom")
    use = {}
    for e in graph["edges"]:
        if e["id"] in plan:
            s_side, t_side = plan[e["id"]]
            use.setdefault((e["source"], s_side), []).append((e["id"], e["target"]))
            use.setdefault((e["target"], t_side), []).append((e["id"], e["source"]))
    ports, reach = {}, {}
    for (nid, side), entries in use.items():
        x, y, w, h = boxes[nid]
        ix, iy, iw, ih = icon_box(boxes[nid])
        key = (lambda o: boxes[o][1] + boxes[o][3] / 2) if side in {"left", "right"} else (lambda o: boxes[o][0] + boxes[o][2] / 2)
        entries.sort(key=lambda t: key(t[1]))
        n = len(entries)
        for i, (eid, _) in enumerate(entries):
            frac = (i + 1) / (n + 1)
            # the connector leaves the cell at the icon's height (sides) or across the icon's width (top, bottom), and
            # runs on to the icon itself through the empty space beside it
            ports[(eid, nid)] = {"right": (x + w, iy + ih * frac), "left": (x, iy + ih * frac), "bottom": (ix + iw * frac, y + h), "top": (ix + iw * frac, y)}[side]
            reach[(eid, nid)] = {"right": (ix + iw, iy + ih * frac), "left": (ix, iy + ih * frac), "bottom": None, "top": None}[side]
    STEP = {"right": (1, 0), "left": (-1, 0), "bottom": (0, 1), "top": (0, -1)}
    DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

    def outside(port, side):
        """First free cell outside the inflated box in the port's direction."""
        c = to_cell(*port)
        dx, dy = STEP[side]
        for _ in range(12):
            if c not in blocked:
                return c
            c = (c[0] + dx, c[1] + dy)
        return c

    def search(start, goal, start_dir):
        best = {}
        heap = [(abs(start[0] - goal[0]) + abs(start[1] - goal[1]), 0, start, start_dir, None)]
        parents = {}
        while heap:
            _, g, c, d, parent = heapq.heappop(heap)
            if (c, d) in best:
                continue
            best[(c, d)] = g
            parents[(c, d)] = parent
            if c == goal:
                path = []
                key = (c, d)
                while key:
                    path.append(key[0]); key = parents[key]
                return path[::-1]
            for nd in DIRS:
                nc = (c[0] + nd[0], c[1] + nd[1])
                if not (0 <= nc[0] < cols and 0 <= nc[1] < rows) or (nc in blocked and nc != goal):
                    continue
                cost = g + 1 + (5 if nd != d else 0) + used.get(nc, 0) + toll.get(nc, 0)
                if (nc, nd) not in best:
                    heapq.heappush(heap, (cost + abs(nc[0] - goal[0]) + abs(nc[1] - goal[1]), cost, nc, nd, (c, d)))
        return None
    routes = {}
    fallback = []
    for e in graph["edges"]:
        if e["id"] not in plan:
            continue
        s_side, t_side = plan[e["id"]]
        a, b = ports[(e["id"], e["source"])], ports[(e["id"], e["target"])]
        ca, cb = outside(a, s_side), outside(b, t_side)
        cells = search(ca, cb, STEP[s_side])
        if not cells:
            pts = [a, b]
            fallback.append(e["id"])
        else:
            mid = [centre(c) for c in cells]
            # Orthogonal stubs between each port and the first/last grid cell: along the side's axis first, then across.
            first, last = mid[0], mid[-1]
            head = [a, (first[0], a[1])] if s_side in {"left", "right"} else [a, (a[0], first[1])]
            tail = [(last[0], b[1]), b] if t_side in {"left", "right"} else [(b[0], last[1]), b]
            pts = head + mid + tail
            if reach.get((e["id"], e["source"])):
                pts = [reach[(e["id"], e["source"])]] + pts
            if reach.get((e["id"], e["target"])):
                pts = pts + [reach[(e["id"], e["target"])]]
            for c in cells:
                used[c] = used.get(c, 0) + 10
                for nx, ny in ((c[0] + 1, c[1]), (c[0] - 1, c[1]), (c[0], c[1] + 1), (c[0], c[1] - 1)):
                    used[(nx, ny)] = used.get((nx, ny), 0) + 2  # keeps parallel connectors a cell apart
        pts = simplify(pts)
        routes[e["id"]] = (pts, None)
    routes["__fallback__"] = fallback
    return routes


def simplify(pts):
    """Drop points that lie on a straight run between their neighbours (and repeated points)."""
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 0.5 and abs(out[-1][1] - p[1]) < 0.5:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (abs(a[0] - b[0]) < 0.5 and abs(b[0] - p[0]) < 0.5) or (abs(a[1] - b[1]) < 0.5 and abs(b[1] - p[1]) < 0.5):
                out[-1] = p; continue
        out.append(p)
    return out


def label_point(pts, obstacles=()):
    """Middle of the longest segment whose middle is not over a node, lifted a little off the line."""
    def inside(pt):
        return any(x - 2 <= pt[0] <= x + w + 2 and y - 2 <= pt[1] <= y + h + 2 for x, y, w, h in obstacles)
    segments = sorted(zip(pts, pts[1:]), key=lambda ab: -(abs(ab[0][0] - ab[1][0]) + abs(ab[0][1] - ab[1][1])))
    for a, b in segments:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if not inside(mid):
            return (mid[0], mid[1] - 4) if abs(a[0] - b[0]) >= abs(a[1] - b[1]) else (mid[0] + 4, mid[1])
    a, b = segments[0]
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 - 4)


LABEL_W = 170       # an edge label wraps at this width, two lines at most
LABEL_LH_EDGE = 11


def boxes_meet(a, b, pad=0):
    return a[0] - pad < b[0] + b[2] and b[0] - pad < a[0] + a[2] and a[1] - pad < b[1] + b[3] and b[1] - pad < a[1] + a[3]


def segment_hits(box, a, b):
    """True when the orthogonal segment a-b passes through `box`."""
    x, y, w, h = box
    if abs(a[1] - b[1]) < 0.5:
        return y <= a[1] <= y + h and min(a[0], b[0]) <= x + w and max(a[0], b[0]) >= x
    return x <= a[0] <= x + w and min(a[1], b[1]) <= y + h and max(a[1], b[1]) >= y


def place_labels(graph, routes, boxes, badge_ids, headers, extent, first=1):
    """A position for every edge label that covers no node, no group title and no other label, preferring spots no
    other connector crosses. A label that fits nowhere becomes a numbered marker on its connector, explained under the
    legend. Returns ({edge id: placement}, [(number, text)])."""
    node_boxes = [boxes[n["id"]] for n in graph["nodes"] if n["id"] in boxes]
    segments = {eid: list(zip(pts, pts[1:])) for eid, (pts, _) in routes.items()}
    placed, keyed, out = [], [], {}
    x0, y0, x1, y1 = extent
    def free(box):
        if box[0] < x0 or box[1] < y0 or box[0] + box[2] > x1 or box[1] + box[3] > y1:
            return False
        return not any(boxes_meet(box, b, 3) for b in node_boxes) and not any(boxes_meet(box, h, 1) for h in headers) and not any(boxes_meet(box, b, 2) for b in placed)
    def crossings(box, eid):
        return sum(1 for other, segs in segments.items() if other != eid for a, b in segs if segment_hits(box, a, b))
    edges = [e for e in graph["edges"] if e["id"] in routes and e["id"] not in badge_ids and " ".join(str(e.get("label") or "").split())]
    for e in sorted(edges, key=lambda e: -len(str(e.get("label")))):
        text = " ".join(str(e["label"]).split())
        lines, cut = wrap(text, DETAIL_SIZE, LABEL_W, 2)
        w = max(text_width(l, DETAIL_SIZE) for l in lines) + 8
        h = len(lines) * LABEL_LH_EDGE + 4
        best = None
        segs = sorted(segments[e["id"]], key=lambda ab: -(abs(ab[0][0] - ab[1][0]) + abs(ab[0][1] - ab[1][1])))
        for a, b in segs if not cut else []:
            horizontal = abs(a[1] - b[1]) < 0.5
            for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
                px, py = a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac
                spots = ([(px - w / 2, py - h - 2), (px - w / 2, py + 2)] if horizontal else [(px + 4, py - h / 2), (px - 4 - w, py - h / 2)])
                for bx, by in spots:
                    box = (bx, by, w, h)
                    if free(box):
                        score = crossings(box, e["id"])
                        if best is None or score < best[0]:
                            best = (score, box)
                if best and best[0] == 0:
                    break
            if best and best[0] == 0:
                break
        if best and best[0] == 0:  # beside its own line and crossed by no other: anything else reads as another line's label
            placed.append(best[1])
            out[e["id"]] = {"lines": lines, "box": best[1]}
            continue
        # A numbered marker on the connector; the text goes under the legend.
        number = first + len(keyed)
        spot = None
        for a, b in segs:
            for frac in (0.5, 0.3, 0.7, 0.15, 0.85):
                cx, cy = a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac
                box = (cx - 8, cy - 8, 16, 16)
                if free(box):
                    spot = (cx, cy); break
            if spot:
                break
        if spot is None:
            a, b = segs[0]
            spot = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        placed.append((spot[0] - 8, spot[1] - 8, 16, 16))
        keyed.append((number, text))
        out[e["id"]] = {"marker": number, "center": spot}
    return out, keyed


def points_to_path(pts):
    return "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def is_public_subnet(group):
    """The graph's `public` flag (set from MapPublicIpOnLaunch by diagram.py); failing that, an English label that says so."""
    if "public" in group:
        return group.get("public") is True
    label = str(group.get("label", "")).lower()
    return "public" in label and "private" not in label


def zone_style(group):
    kind = group.get("kind") or "other"
    if kind == "subnet":
        kind = "public-subnet" if is_public_subnet(group) else "subnet"
    return kind, ZONES.get(kind, ZONES["other"])


def group_depth(groups, gid):
    depth = 0
    while groups[gid].get("parent"):
        gid = groups[gid]["parent"]; depth += 1
    return depth


def renderer_words():
    """The renderer's own English words (legend lines, state tags, the legend heading), for a drawing in another
    language: the localizer translates these with the graph's labels and redraws."""
    out = ["Legend", "Connections", "Architecture", "View", "In other views", "Chip on a node: attached to the aggregate shown with that tag (security groups, route tables)",
           "proposed", "changed", "removal candidate", "unconfirmed", "declared"]
    out += [v[3] for v in STATES.values()] + [v[2] for v in EDGE_STATES.values()] + [v[3] for v in ZONES.values()]
    return list(dict.fromkeys(out))


def graph_texts(graph):
    """Every label a reader sees in the drawing, whole (not wrapped into lines)."""
    out = [graph.get("title") or "", graph.get("subtitle") or ""]
    out += [g.get("label") or "" for g in graph.get("groups", [])]
    out += [n.get(k) or "" for n in graph.get("nodes", []) for k in ("label", "detail")]
    out += [e.get("label") or "" for e in graph.get("edges", [])]
    return [t for t in dict.fromkeys(out) if t.strip()]


def translated_graph(graph, translate):
    g = json.loads(json.dumps(graph))
    for key in ("title", "subtitle"):
        if g.get(key):
            g[key] = translate(g[key])
    for item in g.get("groups", []):
        if item.get("label"):
            item["label"] = translate(item["label"])
    for item in g.get("nodes", []):
        for key in ("label", "detail"):
            if item.get(key):
                item[key] = translate(item[key])
    for item in g.get("edges", []):
        if item.get("label"):
            item["label"] = translate(item["label"])
    return g


# ---------------------------------------------------------------------------
# Views: a drawing with too many components is cut into views along its top-level groups
# ---------------------------------------------------------------------------
SPLIT_NODES = 24           # more components than this, or a layout beyond SPLIT_W x SPLIT_H, is drawn as two or three views
SPLIT_W, SPLIT_H = 1500, 1400
SPLIT_CROSS = 0.3          # ... unless every cut leaves more than this share of the connectors running between views
PART_GAP, PART_HEAD = 44, 30
WRAPPERS = {"region", "cloud", "account"}


def view_sections(graph):
    """(parent group, top-level groups in drawing order, {group: its subtree groups}, {node: section index}). The parent is
    the outermost wrapper (AWS Cloud, account, region) that holds nothing but groups; the sections are its children."""
    groups = {g["id"]: g for g in graph.get("groups", [])}
    children, members = {}, {}
    for g in graph.get("groups", []):
        children.setdefault(g.get("parent") if g.get("parent") in groups else None, []).append(g["id"])
    for n in graph["nodes"]:
        members.setdefault(n.get("group") if n.get("group") in groups else None, []).append(n["id"])
    parent, level = None, children.get(None, [])
    while len(level) == 1 and (groups[level[0]].get("kind") or "") in WRAPPERS and not members.get(level[0]) and children.get(level[0]):
        parent, level = level[0], children[level[0]]
    if parent and (groups[parent].get("kind") or "") == "region":
        kind = lambda c: groups[c].get("kind") or ""  # noqa: E731 - the order Layout.arrange_region draws
        level = [c for c in level if kind(c) == "vpc"] + [c for c in level if kind(c) == "services"] + [c for c in level if kind(c) not in {"vpc", "services"}]

    def subtree(gid):
        out = {gid}
        for c in children.get(gid, []):
            out |= subtree(c)
        return out
    trees = {c: subtree(c) for c in level}
    owner = {}
    for i, c in enumerate(level):
        for gid in trees[c]:
            for nid in members.get(gid, []):
                owner[nid] = i
    for nid in members.get(parent, []) + members.get(None, []):
        owner.setdefault(nid, 0)
    return parent, level, trees, owner


def plan_views(graph, badge_ids):
    """None when one drawing is readable; else (parent, [[section ids] per view], {node: view index}). Consecutive sections
    form a view, so the order of the drawing is kept; the cut that leaves the fewest connectors between views and the
    most even views wins."""
    from itertools import combinations
    n = len(graph["nodes"])
    parent, level, trees, owner = view_sections(graph)
    if len(level) < 2:
        return None
    trial = Layout(graph).run()
    width = max(b[0] + b[2] for b in trial.values()) - min(b[0] for b in trial.values())
    height = max(b[1] + b[3] for b in trial.values()) - min(b[1] for b in trial.values())
    if n <= SPLIT_NODES and width <= SPLIT_W and height <= SPLIT_H:
        return None
    counts = [sum(1 for i in owner.values() if i == k) for k in range(len(level))]
    edges = [e for e in graph["edges"] if e["id"] not in badge_ids and e["source"] in owner and e["target"] in owner]
    best = None
    for parts in (2, 3) if n > 2.5 * SPLIT_NODES else (2,):
        for cuts in combinations(range(1, len(level)), min(parts, len(level)) - 1):
            bounds = (0,) + cuts + (len(level),)
            view_of = {i: v for v in range(len(bounds) - 1) for i in range(bounds[v], bounds[v + 1])}
            sizes = [sum(counts[i] for i in range(bounds[v], bounds[v + 1])) for v in range(len(bounds) - 1)]
            if min(sizes) == 0:
                continue
            cross = sum(1 for e in edges if view_of[owner[e["source"]]] != view_of[owner[e["target"]]])
            cost = 3 * cross + (max(sizes) - min(sizes)) + 6 * max(0, max(sizes) - SPLIT_NODES - 4)
            if best is None or cost < best[0]:
                best = (cost, bounds, view_of)
    if best is None:
        return None
    _, bounds, view_of = best
    cross = sum(1 for e in edges if view_of[owner[e["source"]]] != view_of[owner[e["target"]]])
    if cross > max(4, SPLIT_CROSS * len(edges)):
        return None  # the parts are too entangled: two views would repeat most components as stubs
    views = [level[bounds[v]:bounds[v + 1]] for v in range(len(bounds) - 1)]
    return parent, views, {nid: view_of[i] for nid, i in owner.items()}, trees


def view_graph(graph, parent, sections, trees, node_view, index, badge_ids, say):
    """The part of the graph one view draws. A connector to a component of another view ends at a small copy of that
    component (same icon and name, dotted outline, "View N"), grouped as "In other views", so no line crosses views."""
    groups = {g["id"]: g for g in graph.get("groups", [])}
    nodes = {n["id"]: n for n in graph["nodes"]}
    keep, gid = set(), parent
    while gid:
        keep.add(gid); gid = groups[gid].get("parent") if groups[gid].get("parent") in groups else None
    for section in sections:
        keep |= trees[section]
    mine = [n for n in graph["nodes"] if node_view.get(n["id"], 0) == index]
    ids = {n["id"] for n in mine}
    edges, stubs, refs = [], {}, {}
    for e in graph["edges"]:
        s_in, t_in = e["source"] in ids, e["target"] in ids
        if e["id"] in badge_ids:
            if s_in:
                edges.append(dict(e, drawn_as="badge")); refs[e["target"]] = nodes.get(e["target"]) or {}
            continue
        if s_in and t_in:
            edges.append(e)
        elif s_in or t_in:
            far = e["target"] if s_in else e["source"]
            stub = "~" + far
            if stub not in stubs:
                stubs[stub] = dict({k: v for k, v in nodes[far].items() if k not in {"group", "state", "detail"}}, id=stub, stub_of=far,
                                   detail=f"{say('View')} {node_view.get(far, 0) + 1}", state="elsewhere", group="~views")
            edges.append(dict(e, source=e["source"] if s_in else stub, target=stub if s_in else e["target"]))
    out_groups = [g for g in graph.get("groups", []) if g["id"] in keep]
    if stubs:
        out_groups.append({"id": "~views", "kind": "external", "label": say("In other views"), **({"parent": parent} if parent else {})})
    labels = [str(groups[c].get("label") or "") for c in sections]
    return {"title": graph.get("title"), "groups": out_groups, "nodes": mine + list(stubs.values()), "edges": edges, "refs": refs,
            "heading": f"{say('View')} {index + 1}: " + " · ".join(l for l in labels if l)}


def layout_view(graph, first=1):
    """Geometry of one drawing: node and group boxes, routed connectors, edge labels (numbered from `first`)."""
    nodes_by_id = {**graph.get("refs", {}), **{n["id"]: n for n in graph["nodes"]}}
    badge_edges = [e for e in graph["edges"] if is_badge_edge(e, nodes_by_id)]
    badge_ids = {e["id"] for e in badge_edges}
    boxes = Layout(graph).run()
    routes = route_edges(graph, boxes, badge_ids)
    straight = routes.pop("__fallback__", [])
    missing = [i for i in [g["id"] for g in graph.get("groups", [])] + [n["id"] for n in graph["nodes"]] if i not in boxes]
    if missing:
        raise RuntimeError(f"no geometry for {', '.join(missing[:8])}")
    extent = (min(b[0] for b in boxes.values()), min(b[1] for b in boxes.values()),
              max(b[0] + b[2] for b in boxes.values()), max(b[1] + b[3] for b in boxes.values()))
    headers = group_headers(graph, boxes)
    labels, keyed = place_labels(graph, routes, boxes, badge_ids, headers, extent, first)
    return {"graph": graph, "nodes": nodes_by_id, "badge_edges": badge_edges, "badge_ids": badge_ids, "boxes": boxes, "routes": routes,
            "straight": straight, "extent": extent, "headers": headers, "labels": labels, "keyed": keyed}


def draw_view(view, dx, dy, icon_set, say, notes, drawn=None):
    """SVG fragments of one laid-out drawing, shifted by (dx, dy): groups, connectors, nodes, then edge labels on top.
    `drawn` holds the group and edge ids an earlier view already carries: a repeat (the region around every view, a
    connector between views) is marked data-*-repeat, so each graph id appears once as data-*-id, as the audit expects."""
    drawn = set() if drawn is None else drawn

    def ident(kind, iid):
        key = f"{kind}:{iid}"
        if key in drawn:
            return f'data-{kind}-repeat="{esc(iid)}"'
        drawn.add(key)
        return f'data-{kind}-id="{esc(iid)}"'

    graph, boxes, routes, labels = view["graph"], view["boxes"], view["routes"], view["labels"]
    groups = {g["id"]: g for g in graph.get("groups", [])}
    nodes_by_id, badge_edges = view["nodes"], view["badge_edges"]
    out, top = [], []
    for gid in sorted(groups, key=lambda g: group_depth(groups, g)):
        g = groups[gid]
        x, y, w, h = boxes[gid]
        x += dx; y += dy
        kind, (color, dash, badge, legend) = zone_style(g)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        fill = f' fill="{color}" fill-opacity="0.035"' if kind in {"subnet", "public-subnet", "vpc"} else ' fill="none"'
        marker = f'data-stub-group="{esc(gid)}"' if gid.startswith("~") else ident("group", gid)
        out.append(f'<g {marker} data-kind="{esc(kind)}"><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" stroke="{color}" stroke-width="1.2"{dash_attr}{fill}/>')
        label_x = x + 8
        icon = icon_set.inner(f"group/{badge}") if badge else None
        if icon:
            body, bw, bh = icon
            out.append(f'<g transform="translate({x:.1f},{y:.1f}) scale({24 / bw:.4f})" aria-hidden="true">{body}</g>')
            label_x = x + 30
        elif badge:
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="24" height="24" fill="{color}"/>')
            label_x = x + 30
        lines, truncated = wrap(g.get("label", ""), GROUP_LABEL_SIZE, w - (label_x - x) - 8, 1)
        if truncated:
            notes.append(f"group {gid}: label shortened to fit {w:.0f}px")
        if lines:
            out.append(f'<text x="{label_x:.1f}" y="{y + 16:.1f}" font-size="{GROUP_LABEL_SIZE}" font-weight="600" fill="{INK}">{esc(lines[0])}</text>')
        out.append("</g>")
    out.append(f'<g transform="translate({dx:.1f},{dy:.1f})" fill="none">')
    edge_states, missing_routes = set(), []
    for e in graph["edges"]:
        if e in badge_edges:
            continue
        if e["id"] not in routes:
            missing_routes.append(e["id"]); continue
        pts, _ = routes[e["id"]]
        state = e.get("state") or "observed_configuration"
        color, dash, _ = EDGE_STATES.get(state, EDGE_STATES["observed_configuration"])
        edge_states.add(state if state in EDGE_STATES else "observed_configuration")
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        marker = "arrow-accent" if color == ACCENT else "arrow-soft" if color == SOFT else "arrow"
        label = " ".join(str(e.get("label") or "").split())
        out.append(f'<g {ident("edge", e["id"])}><title>{esc(label)}</title><path d="{points_to_path(pts)}" stroke="{color}" stroke-width="1.4"{dash_attr} marker-end="url(#{marker})"/></g>')
    out.append("</g>")
    top.append(f'<g transform="translate({dx:.1f},{dy:.1f})">')
    for eid, spot in labels.items():
        if "lines" in spot:
            bx, by, bw, bh = spot["box"]
            top.append(f'<g data-label-for="{esc(eid)}"><rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="{PAPER}" fill-opacity="0.92"/>')
            for k, line in enumerate(spot["lines"]):
                top.append(f'<text x="{bx + bw / 2:.1f}" y="{by + 11 + k * LABEL_LH_EDGE:.1f}" font-size="{DETAIL_SIZE}" fill="{MUTED}" text-anchor="middle">{esc(line)}</text>')
            top.append("</g>")
        else:
            cx, cy = spot["center"]
            top.append(f'<g data-label-for="{esc(eid)}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="8" fill="{INK}"/>'
                       f'<text x="{cx:.1f}" y="{cy + 3.5:.1f}" font-size="9" font-weight="700" fill="{PAPER}" text-anchor="middle" font-family="{MONO}">{spot["marker"]}</text></g>')
    top.append("</g>")
    states = set()
    for n in graph["nodes"]:
        x, y, w, h = boxes[n["id"]]
        x += dx; y += dy
        state = n.get("state") or "observed"
        color, dash, sw, _ = STATES.get(state, STATES["observed"])
        states.add(state if state in STATES else "observed")
        label, detail, truncated = node_lines(n)
        if truncated and not n.get("stub_of"):
            notes.append(f"node {n['id']}: text shortened to {MAX_LABEL_LINES}+{MAX_DETAIL_LINES} lines")
        title_text = " ".join(filter(None, [n.get("label"), n.get("detail")]))
        node_attr = f'data-stub-of="{esc(n["stub_of"])}"' if n.get("stub_of") else f'data-node-id="{esc(n["id"])}"'
        out.append(f'<g {node_attr} data-state="{esc(state)}"><title>{esc(title_text)}</title>')
        if state not in PLAIN_STATES:
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" fill="{PAPER}" fill-opacity="0.6" stroke="{color}" stroke-width="{sw}"{dash_attr}/>')
        icon_path, cat_color, tag = icon_for(n)
        ix, iy, _, _ = icon_box((x, y, w, h))
        icon = icon_set.inner(icon_path) if icon_path else None
        if icon:
            body, iw, ih = icon
            out.append(f'<g transform="translate({ix:.1f},{iy:.1f}) scale({ICON / iw:.4f})" aria-hidden="true">{body}</g>')
        else:
            out.append(f'<rect x="{ix:.1f}" y="{iy:.1f}" width="{ICON}" height="{ICON}" rx="4" fill="{cat_color}"/>'
                       f'<text x="{ix + ICON / 2:.1f}" y="{iy + ICON / 2 + 4:.1f}" font-size="{10 if len(tag) > 4 else 12}" font-weight="700" fill="{PAPER}" text-anchor="middle" font-family="{MONO}">{esc(tag)}</text>')
        cx_text = x + w / 2
        ty = iy + ICON + 6 + LABEL_SIZE
        for line in label:
            out.append(f'<text x="{cx_text:.1f}" y="{ty:.1f}" font-size="{LABEL_SIZE}" font-weight="600" fill="{INK}" text-anchor="middle">{esc(line)}</text>'); ty += LABEL_LH
        ty += 3 - (LABEL_LH - NODE_DETAIL_LH) if detail else 0
        for line in detail:
            out.append(f'<text x="{cx_text:.1f}" y="{ty:.1f}" font-size="{NODE_DETAIL_SIZE}" fill="{MUTED}" text-anchor="middle">{esc(line)}</text>'); ty += NODE_DETAIL_LH
        if state in {"proposed", "changed", "removal_candidate", "unresolved", "declared"}:
            tag_text = say({"proposed": "proposed", "changed": "changed", "removal_candidate": "removal candidate", "unresolved": "unconfirmed", "declared": "declared"}[state])
            tw = text_width(tag_text, 8) + 10
            out.append(f'<rect x="{x + w - tw - 6:.1f}" y="{y - 7:.1f}" width="{tw:.1f}" height="14" rx="7" fill="{color}"/>'
                       f'<text x="{x + w - tw / 2 - 6:.1f}" y="{y + 3.3:.1f}" font-size="8" font-weight="700" fill="{PAPER}" text-anchor="middle">{esc(tag_text)}</text>')
        mine = [e for e in badge_edges if e["source"] == n["id"]]
        chips = []
        for e in mine:
            target = nodes_by_id.get(e["target"]) or {}
            _, chip_color, chip_tag = icon_for(target)
            chips.append((e, target, chip_color, chip_tag, text_width(chip_tag, 8) + 10))
        cx = x + (w - (sum(c[4] for c in chips) + 4 * max(0, len(chips) - 1))) / 2
        for e, target, chip_color, chip_tag, cw in chips:
            out.append(f'<g {ident("edge", e["id"])}><title>{esc(" ".join(str(e.get("label") or "").split()) + " → " + str(target.get("label") or e["target"]))}</title>'
                       f'<rect x="{cx:.1f}" y="{y + h - 15:.1f}" width="{cw:.1f}" height="11" rx="2" fill="{chip_color}" fill-opacity="0.12" stroke="{chip_color}" stroke-width="0.8"/>'
                       f'<text x="{cx + cw / 2:.1f}" y="{y + h - 6.8:.1f}" font-size="7.5" font-weight="700" fill="{chip_color}" text-anchor="middle" font-family="{MONO}">{esc(chip_tag)}</text></g>')
            cx += cw + 4
        out.append("</g>")
    kinds = [zone_style(groups[gid]) for gid in groups]
    return out, top, states, edge_states, kinds, missing_routes


def view_checks(view):
    """Geometry QA of one laid-out drawing (see render)."""
    graph, boxes, routes, labels, headers, badge_edges = view["graph"], view["boxes"], view["routes"], view["labels"], view["headers"], view["badge_edges"]
    outside, overlaps, loose = [], [], []
    for n in graph["nodes"]:
        x, y, w, h = boxes[n["id"]]
        if n.get("group"):
            gx, gy, gw, gh = boxes[n["group"]]
            if not (gx - 1 <= x and gy - 1 <= y and x + w <= gx + gw + 1 and y + h <= gy + gh + 1):
                outside.append(n["id"])
    nodes = graph["nodes"]
    for i in range(len(nodes)):
        ax, ay, aw, ah = boxes[nodes[i]["id"]]
        for j in range(i + 1, len(nodes)):
            bx, by, bw, bh = boxes[nodes[j]["id"]]
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                overlaps.append([nodes[i]["id"], nodes[j]["id"]])
    for e in graph["edges"]:
        if e in badge_edges or e["id"] not in routes:
            continue
        pts = routes[e["id"]][0]

        def touches(pt, box, tol=1):
            x, y, w, h = box
            return x - tol <= pt[0] <= x + w + tol and y - tol <= pt[1] <= y + h + tol
        if not (touches(pts[0], boxes[e["source"]]) and touches(pts[-1], boxes[e["target"]])):
            loose.append(e["id"])
    node_boxes_all = [boxes[n["id"]] for n in graph["nodes"]]
    label_boxes = [(eid, sp["box"] if "box" in sp else (sp["center"][0] - 8, sp["center"][1] - 8, 16, 16)) for eid, sp in labels.items()]
    covered = [eid for eid, b in label_boxes if any(boxes_meet(b, nb, 1) for nb in node_boxes_all) or any(boxes_meet(b, hb, 0) for hb in headers)]
    clashing = sorted({eid for i, (eid, b) in enumerate(label_boxes) for other, c in label_boxes[i + 1:] if boxes_meet(b, c, 0)})
    through_titles = sorted({e["id"] for e in graph["edges"] if e["id"] in routes and e not in badge_edges
                             for a, b in zip(routes[e["id"]][0], routes[e["id"]][0][1:]) for hb in headers if segment_hits((hb[0] + 2, hb[1] + 4, hb[2] - 4, hb[3] - 8), a, b)})
    # a connector that runs through another node's cell (its icon or its text) would hide it
    through_nodes = sorted({e["id"] for e in graph["edges"] if e["id"] in routes and e not in badge_edges
                            for a, b in zip(routes[e["id"]][0], routes[e["id"]][0][1:])
                            for n in graph["nodes"] if n["id"] not in {e["source"], e["target"]}
                            and segment_hits((boxes[n["id"]][0] + 2, boxes[n["id"]][1] + 2, boxes[n["id"]][2] - 4, boxes[n["id"]][3] - 4), a, b)})
    return {"outside": outside, "overlaps": overlaps, "loose": loose, "covered": covered, "clashing": clashing, "through_titles": through_titles,
            "through_nodes": through_nodes, "straight": view["straight"]}


def render(graph, output, name, title=None, note=None, icons=None, language="en", words=None):
    """Write <name>.aws.svg, <name>.aws.html, <name>.render.json and diagram-qa.json; return the QA record. `words` maps
    the renderer's own English words (legend lines, state tags) to the drawing's language. A graph with more components
    than one readable drawing holds is drawn as two or three views, one under the other, in the same file."""
    say = (lambda text: (words or {}).get(text, text))
    output.mkdir(parents=True, exist_ok=True)
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    badge_ids = {e["id"] for e in graph["edges"] if is_badge_edge(e, nodes_by_id)}
    plan = plan_views(graph, badge_ids)
    if plan:
        parent, sections, node_view, trees = plan
        graphs = [view_graph(graph, parent, part, trees, node_view, i, badge_ids, say) for i, part in enumerate(sections)]
    else:
        graphs = [graph]
    views, first = [], 1
    for g in graphs:
        view = layout_view(g, first)
        first += len(view["keyed"])
        views.append(view)
    qa = {"status": "pass", "checks": {}, "notes": []}
    icon_set = IconSet(icons)
    out, top, states, edge_states, kinds_seen, keyed, missing_routes, drawn = [], [], set(), set(), [], [], [], set()
    width = max(v["extent"][2] - v["extent"][0] for v in views) + 2 * MARGIN
    y = TITLE_H + MARGIN
    for i, view in enumerate(views):
        x0, y0, x1, y1 = view["extent"]
        if len(views) > 1:
            out.append(f'<g data-view="{i + 1}"><text x="{MARGIN}" y="{y + 14:.1f}" font-size="13" font-weight="700" fill="{INK}">{esc(view["graph"]["heading"])}</text>'
                       f'<line x1="{MARGIN}" y1="{y + 22:.1f}" x2="{width - MARGIN:.1f}" y2="{y + 22:.1f}" stroke="#D5DBDB" stroke-width="1"/></g>')
            y += PART_HEAD
        o, t, st, es, kinds, mr = draw_view(view, MARGIN - x0, y - y0, icon_set, say, qa["notes"], drawn)
        out += o; top += t; states |= st; edge_states |= es; missing_routes += mr; keyed += view["keyed"]
        for kind, style in kinds:
            if kind not in [k for k, _ in kinds_seen]:
                kinds_seen.append((kind, style))
        y += (y1 - y0) + (PART_GAP if i < len(views) - 1 else 0)
    # Legend
    legend = []
    lx = MARGIN
    y_cursor = y + 28
    legend.append(f'<text x="{MARGIN}" y="{y_cursor:.1f}" font-size="10" font-weight="700" fill="{INK}">{esc(say("Legend"))}</text>')
    y_cursor += 14
    row_items = [("node", s, STATES[s]) for s in sorted(states, key=lambda s: list(STATES).index(s))]
    row_items += [("edge", s, EDGE_STATES[s]) for s in sorted(edge_states, key=lambda s: list(EDGE_STATES).index(s))]
    for kind, item, style in row_items:
        if kind == "node":
            color, dash, sw, text = style
            if item in PLAIN_STATES:
                legend.append(f'<rect x="{lx + 5}" y="{y_cursor - 10:.1f}" width="12" height="12" rx="2" fill="#879196"/>')
            else:
                dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
                legend.append(f'<rect x="{lx}" y="{y_cursor - 9:.1f}" width="22" height="12" rx="3" fill="{PAPER}" stroke="{color}" stroke-width="{sw}"{dash_attr}/>')
        else:
            color, dash, text = style
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            legend.append(f'<line x1="{lx}" y1="{y_cursor - 3:.1f}" x2="{lx + 22}" y2="{y_cursor - 3:.1f}" stroke="{color}" stroke-width="1.3"{dash_attr} marker-end="url(#arrow)"/>')
        legend.append(f'<text x="{lx + 28}" y="{y_cursor:.1f}" font-size="9.5" fill="{MUTED}">{esc(say(text))}</text>')
        y_cursor += 15
    for kind, (color, dash, badge, text) in kinds_seen:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        legend.append(f'<rect x="{lx}" y="{y_cursor - 9:.1f}" width="22" height="12" fill="none" stroke="{color}" stroke-width="1.2"{dash_attr}/>')
        legend.append(f'<text x="{lx + 28}" y="{y_cursor:.1f}" font-size="9.5" fill="{MUTED}">{esc(say(text))}</text>')
        y_cursor += 15
    if any(v["badge_edges"] for v in views):
        legend.append(f'<rect x="{lx}" y="{y_cursor - 9:.1f}" width="22" height="11" rx="2" fill="{RED}" fill-opacity="0.12" stroke="{RED}" stroke-width="0.8"/>'
                      f'<text x="{lx + 11}" y="{y_cursor - 0.8:.1f}" font-size="7.5" font-weight="700" fill="{RED}" text-anchor="middle" font-family="{MONO}">SG</text>'
                      f'<text x="{lx + 28}" y="{y_cursor:.1f}" font-size="9.5" fill="{MUTED}">{esc(say("Chip on a node: attached to the aggregate shown with that tag (security groups, route tables)"))}</text>')
        y_cursor += 15
    if keyed:
        y_cursor += 6
        legend.append(f'<text x="{MARGIN}" y="{y_cursor:.1f}" font-size="10" font-weight="700" fill="{INK}">{esc(say("Connections"))}</text>')
        y_cursor += 14
        for number, text in keyed:
            lines, _ = wrap(text, 9.5, max(400, width - 2 * MARGIN - 40), 3)
            legend.append(f'<circle cx="{lx + 8}" cy="{y_cursor - 3.5:.1f}" r="7" fill="{INK}"/><text x="{lx + 8}" y="{y_cursor - 0.5:.1f}" font-size="8.5" font-weight="700" fill="{PAPER}" text-anchor="middle" font-family="{MONO}">{number}</text>')
            for k, line in enumerate(lines):
                legend.append(f'<text x="{lx + 22}" y="{y_cursor + k * 12:.1f}" font-size="9.5" fill="{MUTED}">{esc(line)}</text>')
            y_cursor += 12 * len(lines) + 4
    if note:
        legend.append(f'<text x="{MARGIN}" y="{y_cursor + 4:.1f}" font-size="9.5" fill="{SOFT}">{esc(say(note))}</text>')
        y_cursor += 16
    height = y_cursor + MARGIN
    heading = say(title or graph.get("title") or "Architecture")
    subtitle = graph.get("subtitle") or ""
    head = (f'<text x="{MARGIN}" y="30" font-size="18" font-weight="700" fill="{INK}">{esc(heading)}</text>'
            + (f'<text x="{MARGIN}" y="48" font-size="11" fill="{MUTED}">{esc(subtitle)}</text>' if subtitle else ""))
    defs = ('<defs>'
            f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{MUTED}"/></marker>'
            f'<marker id="arrow-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{ACCENT}"/></marker>'
            f'<marker id="arrow-soft" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{SOFT}"/></marker>'
            '</defs>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" lang="{esc(language)}" aria-labelledby="dg-title dg-desc" data-renderer="aws_diagram" data-views="{len(views)}">'
           f'<title id="dg-title">{esc(heading)}</title><desc id="dg-desc">{esc(subtitle or note or heading)}</desc>{defs}'
           f'<rect width="100%" height="100%" fill="{PAPER}"/><g font-family="{SANS}">{head}{"".join(out)}{"".join(top)}{"".join(legend)}</g></svg>\n')
    # Geometry QA per view: nodes inside their group, no node overlap, connectors end at their nodes and never run through
    # another node, labels clear of nodes, titles and each other.
    merged = {}
    for view in views:
        for key, value in view_checks(view).items():
            merged.setdefault(key, []).extend(value)
    qa["checks"] = {"nodes_inside_their_group": {"status": "pass" if not merged["outside"] else "fail", "outside": merged["outside"]},
                    "labels_clear": {"status": "pass" if not (merged["covered"] or merged["clashing"]) else "fail", "over_nodes_or_titles": merged["covered"],
                                     "over_each_other": merged["clashing"], "numbered": [n for n, _ in keyed]},
                    "group_titles_clear": {"status": "pass" if not merged["through_titles"] else "warn", "crossed_by": sorted(set(merged["through_titles"]))},
                    "connectors_clear_of_nodes": {"status": "pass" if not merged["through_nodes"] else "fail", "through": sorted(set(merged["through_nodes"]))},
                    "readable_width": {"status": "pass" if width <= 2400 else "warn", "width": round(width),
                                       "note": "embedded at page width a drawing wider than 2400 px shows text below 6 px"},
                    "no_node_overlap": {"status": "pass" if not merged["overlaps"] else "fail", "overlaps": merged["overlaps"][:20]},
                    "edges_end_at_their_nodes": {"status": "pass" if not (merged["loose"] or missing_routes) else "fail", "loose": merged["loose"], "not_routed": missing_routes},
                    "connectors_avoid_nodes": {"status": "pass" if not merged["straight"] else "fail", "drawn_straight": merged["straight"]},
                    "labels_fit": {"status": "pass", "shortened": [n for n in qa["notes"]]},
                    "views": {"count": len(views), "headings": [v["graph"].get("heading") for v in views] if len(views) > 1 else [],
                              "note": f"more than {SPLIT_NODES} components or a layout beyond {SPLIT_W}x{SPLIT_H} px is drawn as views"},
                    "icons": {"source": str(icon_set.root) if icon_set.root else None,
                              "status": "official" if icon_set.root else "service_tags",
                              "nodes_without_icon": [n["id"] for n in graph["nodes"] if not (icon_for(n)[0] and icon_set.inner(icon_for(n)[0]))]}}
    if merged["outside"] or merged["overlaps"] or merged["loose"] or missing_routes or merged["straight"] or merged["covered"] or merged["clashing"] or merged["through_nodes"]:
        qa["status"] = "fail"
    qa["size"] = {"width": round(width), "height": round(height), "nodes": len(graph["nodes"]), "groups": len(graph.get("groups", [])), "edges": len(graph["edges"]),
                  "badge_edges": len(badge_ids), "views": len(views)}
    qa["files"] = {"svg": f"{name}.aws.svg", "html": f"{name}.aws.html", "render": f"{name}.render.json"}
    (output / f"{name}.aws.svg").write_text(svg, encoding="utf-8")
    # The exact inputs of this drawing, so a report in another language can redraw it from translated labels instead of
    # translating wrapped line fragments (localize_artifacts.py apply does this).
    (output / f"{name}.render.json").write_text(json.dumps({"schema_version": "1.0", "name": name, "title": heading, "note": note or "",
                                                             "language": language, "graph": graph}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    html = (f'<!doctype html><html lang="{esc(language)}"><head><meta charset="utf-8"><title>{esc(heading)}</title>'
            f'<style>body{{margin:0;background:#F2F3F3;font-family:{SANS}}} .wrap{{padding:24px;overflow:auto}} svg{{background:#fff;box-shadow:0 1px 4px rgba(35,47,62,.15);max-width:none}}</style>'
            f'</head><body><div class="wrap">{svg}</div></body></html>\n')
    (output / f"{name}.aws.html").write_text(html, encoding="utf-8")
    (output / "diagram-qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return qa


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="as-is")
    parser.add_argument("--title")
    parser.add_argument("--note", help="one line under the legend, e.g. what the arrows mean")
    parser.add_argument("--icons", type=Path, help="aws-icons folder of the aws-diagram-design skill; default: found beside this skill or in the client's skills folder")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {".", ".."}:
        parser.error("name must be a filename stem")
    try:
        graph = json.loads(args.graph.read_text(encoding="utf-8"))
        roots = icon_roots(args.icons)
        qa = render(graph, args.output, args.name, args.title, args.note, roots[0] if roots else None, args.language)
        print(json.dumps({"status": qa["status"], "svg": qa["files"]["svg"], "icons": qa["checks"]["icons"]["status"], "size": qa["size"]}, ensure_ascii=False))
        return 0 if qa["status"] == "pass" else 1
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        print(f"AWS diagram stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
