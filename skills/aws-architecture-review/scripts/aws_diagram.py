#!/usr/bin/env python3
"""Draw the AWS-styled architecture diagram from a diagram graph, without an agent redraw.

The layout is the one an architect draws by hand: in a region, the VPCs side by side and the regional services beside
them; in a VPC, the Availability Zones as columns of equal width with their subnets stacked in aligned tiers (public
first), then one band per group of resources deployed across several subnets, then the VPC-level components (gateways
on top, endpoints and aggregates below). Nodes are drawn as in the official AWS architecture diagrams: the 48 px service
icon with the name under it and at most two short detail lines, no box (a dashed or coloured outline only for proposed,
changed, declared or unconfirmed components); zones are the official containers (region, VPC, AZ, subnet) with their
badges and fills. Grouping resources are drawn as containers around what they apply to: a security group is a red
dashed frame around each attached component, a target group or Auto Scaling group a dashed container around its
members when they sit in one zone. Members of a group are ordered and placed by the position of what they connect to,
so connectors run short and straight; connectors are orthogonal with rounded bends, leave and enter the icons on the
side that needs the fewest bends, keep apart from each other, and carry their labels beside their own line or as
numbered markers explained under the legend. A graph too large for one readable drawing is drawn as two or three views
along its top-level groups (see plan_views); a connector to a component of another view is a chip on the component
("-> Secrets Manager · View 2"), not a line. A title and a legend complete the drawing. Every node, group and edge
keeps its graph id as data-node-id / data-group-id / data-edge-id exactly once, so `diagram.py --audit-graph` passes on
the result. Icons are read from the aws-diagram-design companion skill (assets/aws-icons) when it is installed beside
this skill or in the client's skills folder; without it, nodes carry a coloured service tag instead of an icon.

Geometry is checked here, not by looking at a PNG: every label fits its box, every node lies inside its group, no two
nodes overlap, every connector ends at its nodes and runs through no other node or group title. diagram-qa.json
records the checks (a failing check fails the render) and a `readability` measurement: connector crossings, overlaps,
bends, total and longest connector length, drawing size.

  aws_diagram.py --graph diagram-graph.json --output DIR --name as-is [--title "..."] [--note "..."] [--icons DIR]

Text in the drawing (title, legend, group and node labels) is the English draft unless the graph already carries the
output language; localize_artifacts.py redraws translated drawings from <name>.render.json with renderer_words().
"""
import argparse
import hashlib
import heapq
import json
import math
from pathlib import Path
import re
import sys

SANS = "'Amazon Ember','Helvetica Neue',Helvetica,Arial,'Apple SD Gothic Neo','Noto Sans KR',sans-serif"
MONO = "'Amazon Ember Mono',ui-monospace,SFMono-Regular,Menlo,monospace"
INK, MUTED, SOFT, ACCENT, RED, PAPER = "#232F3E", "#545B64", "#7D8998", "#EC7211", "#DD344C", "#FFFFFF"
TEXT_2 = "#414D5C"     # edge labels and legend text: darker than MUTED, on white plates
RULE = "#D5DBDB"       # hairlines (legend separator)

# Every coordinate and size of the layout is a multiple of GRID, and the router's cells are GRID wide with their centres
# on that grid, so a connector between two aligned icons is one straight line.
GRID = 8
# A node is drawn the way the official AWS architecture diagrams draw it: the 48 px icon, the name centred under it and
# at most two short detail lines, no box. NODE_W is the width of that cell (the text column), which connectors and edge
# labels keep clear of; connectors attach to the icon.
NODE_W = 160
NODE_TOP = 8           # space above the icon inside the cell (room for a state tag)
ICON = 48
TEXT_W = NODE_W - 8
LABEL_SIZE, LABEL_LH = 12, 15            # node name: 12 px semibold ink
NODE_DETAIL_SIZE, NODE_DETAIL_LH = 10, 12.5
DETAIL_SIZE, EDGE_LABEL_LH = 10, 12.5    # edge labels
MAX_LABEL_LINES, MAX_DETAIL_LINES = 3, 2
ID_SIZE, ID_LH = 9, 12                   # resource ids, in mono
HOP = 5                                  # radius of the bridge a less important connector makes where it crosses another
GROUP_LABEL_SIZE, TITLE_LH = 12, 15
TITLE_MIN_W = 360                         # a zone title wider than its content and this wraps to two lines
WRAP_LABEL_SIZE = 10.5                   # title of a security group frame or a target group container
TITLE_H = 64
MARGIN = 24

# Zone containers: border colour, dash pattern, group badge file (aws-icons/group/<file>), legend text. Official AWS
# convention; FILLS are the official light fills of the subnet containers.
ZONES = {
    "region": ("#00A4A6", "4,4", "Region_32.svg", "Region"),
    "vpc": ("#8C4FFF", None, "Virtual-private-cloud-VPC_32.svg", "VPC"),
    "az": ("#00A4A6", "4,4", None, "Availability Zone"),
    "subnet": ("#00A4A6", None, "Private-subnet_32.svg", "Private subnet"),
    "public-subnet": ("#7AA116", None, "Public-subnet_32.svg", "Public subnet"),
    "subnet-span": ("#00A4A6", "2,4", None, "Resources spanning several subnets"),
    "services": ("#7D8998", "4,4", None, "Regional services outside the VPC"),
    "account": ("#E7157B", None, "AWS-Account_32.svg", "AWS account"),
    "cloud": ("#232F3E", None, "AWS-Cloud_32.svg", "AWS Cloud"),
    "security-group": ("#DD344C", "4,4", None, "Security group"),
    "target-group": ("#8C4FFF", "4,4", None, "Target group: the components registered behind it"),
    "autoscaling": ("#ED7100", "4,4", "Auto-Scaling-group_32.svg", "Auto Scaling group"),
    "other": ("#545B64", None, None, "Group"),
}
FILLS = {"public-subnet": "#F2F6E8", "subnet": "#E6F6F7", "subnet-span": "#F4FAFB"}
WRAP_KINDS = {"security-group", "target-group", "autoscaling"}  # drawn around the components they apply to
# Node states: stroke colour, dash pattern, stroke width, legend text. Observed and retained are the plain icon.
STATES = {
    "observed": (INK, None, 1, "Observed in the account (configuration read by API)"),
    "retained": (INK, None, 1, "Retained as it is"),
    "declared": (MUTED, "3,3", 1, "Declared in a template or source; not observed as a resource"),
    "changed": (ACCENT, None, 1, "Existing component the proposal changes"),
    "proposed": (ACCENT, "4,3", 1.2, "Proposed; does not exist yet"),
    "removal_candidate": (RED, "4,3", 1.2, "Candidate for removal"),
    "unresolved": (SOFT, "3,3", 1, "Could not be confirmed from the evidence"),
}
PLAIN_STATES = {"observed", "retained"}  # drawn without an outline, as in the official diagrams
TAG_ONLY = {"changed"}                   # an existing component the proposal changes: its tag says so, no outline
STATE_TAGS = {"proposed": "proposed", "changed": "changed", "removal_candidate": "removal candidate", "unresolved": "unconfirmed", "declared": "declared"}
# Edges to these aggregate nodes (every resource is attached to some security group and route table) are not routed
# lines: a security group attachment is a frame around the source node, a route table attachment a chip on it. The edge
# keeps its id on the frame or chip.
BADGE_TARGETS = {"ec2.security-group", "ec2.route-table", "elbv2.target-group:aggregate"}
EDGE_STATES = {
    "observed_configuration": (MUTED, None, "Configured path (not measured traffic)"),
    "observed": (MUTED, None, "Configured path (not measured traffic)"),
    "declared": (MUTED, "4,3", "Declared in a template or source"),
    "proposed": (ACCENT, "5,3", "Proposed path"),
    "unresolved": (SOFT, "2,3", "Unconfirmed path"),
}
LINK_LEGEND = "Chip under a component: a connection to a component drawn in another view (→ to it, ← from it)"
CHIP_LEGEND = "Chip on a node: attached to the aggregate shown with that tag (route tables)"

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
NBSP = "\u00a0"
BREAKS = "-_/"     # an over-long word may be split after one of these
MIN_PIECE = 4      # ... never leaving a piece shorter than this
RAW_ID = re.compile(r"^(?:i|igw|nat|vpce|eni|vol|sg|rtb|subnet|vpc|eipalloc|eipassoc|tgw|tgw-attach|acl|ami|snap|lt|pcx|vgw|cgw|dopt|fl)-[0-9a-f]{8,17}$")


def char_width(ch, size):
    o = ord(ch)
    if 0x1100 <= o <= 0x11FF or 0x3000 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
        return size * 1.0
    if ch in "iljtfr.,:;|!'’`":
        return size * 0.32
    if ch in " " + NBSP:
        return size * 0.28
    if ch.isdigit():
        return size * 0.58
    if ch.isupper():
        return size * 0.70 if ch in "MW" else size * 0.64
    if ch.islower():
        return size * 0.60 if ch in "mw" else size * 0.54
    return size * 0.5


def text_width(text, size, bold=False):
    return sum(char_width(c, size) for c in text) * (1.07 if bold else 1.03)


def split_word(word, ok):
    """The longest head of `word` that ends after - _ or /, leaves at least MIN_PIECE characters on both sides and
    passes `ok`; None when there is none."""
    best = None
    for i in range(MIN_PIECE, len(word) - MIN_PIECE + 1):
        if word[i - 1] in BREAKS and ok(word[:i]):
            best = i
    return word[:best] if best else None


def cut_word(word, fits):
    """A word with no usable separator, cut by characters (the last resort), still leaving MIN_PIECE characters."""
    cut = len(word)
    while cut > 1 and not fits(word[:cut]):
        cut -= 1
    if 0 < len(word) - cut < MIN_PIECE:
        cut = max(1, len(word) - MIN_PIECE)
    return word[:cut]


def place_long(word, current, fits):
    """(finished lines, open line) after placing a word wider than a whole line: split at - _ / (else by characters),
    its head on the current line only when that saves a line, so a long name otherwise starts on a line of its own."""
    def run(line):
        done, rest = [], word
        while rest:
            prefix = f"{line} " if line else ""
            head = split_word(rest, lambda h: fits(prefix + h))
            if head is None and line:
                done.append(line)
                line = ""
                continue
            if head is None:
                head = cut_word(rest, fits)
            rest = rest[len(head):]
            line = prefix + head
            if rest:
                done.append(line)
                line = ""
                if fits(rest):
                    line, rest = rest, ""
        return done, line
    done, line = run("")
    alone = (([current] if current else []) + done, line)
    shared = run(current) if current else alone
    return shared if len(shared[0]) < len(alone[0]) else alone


def wrap(text, size, width, max_lines, bold=False):
    """Lines that fit `width`. Breaks at spaces first; a word wider than a whole line is split after - _ or / (only
    there, and never leaving a piece shorter than four characters); a word without such a place is cut by characters.
    A middle dot stays at the end of the line before a break, "× 4" stays together, and a last line shorter than four
    characters takes the word before it. Returns (lines, truncated)."""
    text = " ".join(str(text or "").split())
    if not text:
        return [], False

    def fits(s):
        return text_width(s, size, bold) <= width
    text = re.sub(r"× (?=\d)", "×" + NBSP, text.replace(" · ", NBSP + "· "))
    lines, current = [], ""
    for word in text.split(" "):
        trial = f"{current} {word}" if current else word
        if fits(trial):
            current = trial
        elif fits(word):
            lines.append(current)
            current = word
        else:
            done, current = place_long(word, current, fits)
            lines += done
    if current:
        lines.append(current)
    if len(lines) >= 2 and len(lines[-1].replace(NBSP, " ").strip()) < MIN_PIECE and " " in lines[-2]:
        head, moved = lines[-2].rsplit(" ", 1)
        if fits(f"{moved} {lines[-1]}"):
            lines[-2:] = [head, f"{moved} {lines[-1]}"]
    lines = [line.replace(NBSP, " ") for line in lines]
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_width(last + "…", size, bold) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ·-_/,") + "…"
    return lines, truncated


def short_id(native):
    """An AWS resource id shortened for a drawing: the prefix, the first and last four hex digits (sg-0313…8235)."""
    m = re.match(r"^([a-z]+(?:-[a-z]+)*)-([0-9a-f]{8,17})$", str(native or ""))
    return f"{m.group(1)}-{m.group(2)[:4]}…{m.group(2)[-4:]}" if m and len(m.group(2)) > 8 else str(native or "")


def detail_ids(text):
    """The raw resource ids visible_detail() takes out of a detail line, shortened, for the node's id line."""
    return [short_id(p.strip()) for p in str(text or "").split(" · ") if RAW_ID.match(p.strip())]


def visible_detail(text, label=""):
    """The detail line without raw resource ids (i-0f7..., nat-08a...) and, under a label that already counts the
    resources ("Security groups × 11"), without the word "Aggregated". Both stay in the node's <title>."""
    parts = [p for p in str(text or "").split(" · ") if p.strip() and not RAW_ID.match(p.strip())]
    if "×" in str(label) and parts and parts[0].strip().lower() == "aggregated":
        parts = parts[1:]
    return " · ".join(parts)


def fit_line(text, size, width, bold=False):
    """`text` on one line, shortened with "…" when it is wider than `width`."""
    lines, _ = wrap(text, size, width, 1, bold)
    return lines[0] if lines else ""


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


def node_type(node):
    kind = node.get("type") or ""
    return CFN_TO_TYPE.get(kind, kind)


def icon_for(node):
    kind = node_type(node)
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
# Region: VPCs side by side, then the regional-services group, then the other groups and loose nodes, in rows. VPC: the
# gateways on top, Availability Zones as columns of one width (subnets stacked in tiers aligned across the columns,
# public first), then one band per subnet span (resources deployed in several subnets), then the VPC-level nodes in a
# row. Other groups: a grid. Sizes are computed bottom-up, positions top-down, every value on the GRID; then arrange()
# picks each group's column count and member order and slides rows under what they connect to (align_rows).
def esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def node_lines(node, width=TEXT_W):
    label, t1 = wrap(node.get("label", ""), LABEL_SIZE, width, MAX_LABEL_LINES, True)
    detail, t2 = wrap(visible_detail(node.get("detail", ""), node.get("label", "")), NODE_DETAIL_SIZE, width, MAX_DETAIL_LINES)
    return label, detail, t1 or t2


def id_line(node, width=TEXT_W):
    """The node's resource id, shortened, in mono under its text (only when the detail line carried a raw id)."""
    ids = detail_ids(node.get("detail", ""))
    return fit_line(", ".join(ids), ID_SIZE, width) if ids else ""


BADGE_ROW = 16     # a row of route-table chips under the node text
LINK_ROW = 24      # one chip per connector to a component of another view
LINK_MAX_W = 224   # a node cell widens up to this to show the name on such a chip
GAP = 48           # between siblings, vertically: a corridor for three or four connectors
GAP_X = 96         # between siblings, horizontally: room for an arrow and a short label between two cells
MAX_ROW_W = 1400   # a row of groups wraps beyond this width, so a drawing stays readable at page width
PAD = 16           # inside a group, left/right/bottom
HEAD = 40          # inside a group, top: the title band (24) and a free row a connector can turn in before it leaves the group
WRAP_PAD = 8       # inside a security group frame, target group or Auto Scaling group container
WRAP_HEAD = 24     # its title band (40 when it shows a detail line)
STRETCH = 0.6      # groups side by side get the row's height when they are at least this share of it
_SIZES = {}


def snap(value, step=GRID):
    return int(math.ceil(value / step - 1e-9)) * step


def node_size(node, badges=0, links=0):
    key = (node.get("label"), node.get("detail"), badges, links)
    if key not in _SIZES:
        label, detail, _ = node_lines(node)
        text_h = len(label) * LABEL_LH + (len(detail) * NODE_DETAIL_LH + 3 if detail else 0) + (ID_LH if id_line(node) else 0)
        _SIZES[key] = (NODE_W, snap(NODE_TOP + ICON + 6 + text_h + 4 + (BADGE_ROW if badges else 0) + LINK_ROW * links))
    return _SIZES[key]


def icon_box(box):
    """The icon inside a node cell: centred, at the top."""
    x, y, w, h = box
    return (x + (w - ICON) / 2, y + NODE_TOP, ICON, ICON)


def is_badge_edge(edge, nodes):
    target = nodes.get(edge["target"]) or {}
    hint = str(edge.get("drawn_as") or "").lower()
    return "badge" in hint or target.get("type") in BADGE_TARGETS


def subnet_order(group, index=0):
    """Public subnets first, then by address (app subnets usually come before database subnets), so the order does not
    depend on the language of the labels; without an address, the graph's own order decides."""
    import ipaddress
    found = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b", str(group.get("label", "")))
    try:
        net = int(ipaddress.ip_network(found.group(1), strict=False).network_address) if found else 1 << 40
    except ValueError:
        net = 1 << 40
    return (0 if is_public_subnet(group) else 1, net, index)


def grid(items, columns):
    """Place (id, w, h) items in rows of `columns`; returns ({id: (x, y)}, width, height, [[ids] per row])."""
    pos, lines, x, y, row_h, width = {}, [], 0, 0, 0, 0
    for i, (iid, w, h) in enumerate(items):
        if i and i % columns == 0:
            x, y, row_h = 0, y + row_h + GAP, 0
        if x == 0:
            lines.append([])
        lines[-1].append(iid)
        pos[iid] = (x, y)
        x += w + GAP_X
        row_h = max(row_h, h)
        width = max(width, x - GAP_X)
    return pos, width, (y + row_h) if items else 0, lines


def rows(items, max_width=MAX_ROW_W):
    """Place (id, w, h) items left to right, starting a new row when the next one would pass `max_width`."""
    pos, lines, x, y, row_h, width = {}, [], 0, 0, 0, 0
    for iid, w, h in items:
        if x and x + w > max_width:
            x, y, row_h = 0, y + row_h + GAP, 0
        if x == 0:
            lines.append([])
        lines[-1].append(iid)
        pos[iid] = (x, y)
        x += w + GAP_X
        row_h = max(row_h, h)
        width = max(width, x - GAP_X)
    return pos, width, (y + row_h) if items else 0, lines


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


def column_options(n, kind):
    """Column counts worth trying for a grid of n items."""
    if n < 2 or kind == "az":
        return []
    fit = max(1, (MAX_ROW_W + GAP_X) // (NODE_W + GAP_X))
    out = {1, 2, 3, -(-n // 2), -(-n // 3), columns_for(n, kind), min(n, fit)}
    return sorted(c for c in out if 1 <= c <= min(n, fit))


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
    """Boxes of every group and node. `cols` overrides a group's column count, `order` the order of its members (nodes
    and the frames around them); arrange() searches both."""

    def __init__(self, graph, cols=None, order=None):
        self.graph = graph
        self.groups = {g["id"]: g for g in graph.get("groups", [])}
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.cols, self.order = cols or {}, order or {}
        self.children = {gid: [] for gid in self.groups}      # child group ids
        self.members = {gid: [] for gid in self.groups}       # node ids directly in the group
        self.roots = []
        for gid, g in self.groups.items():
            (self.children[g["parent"]] if g.get("parent") in self.groups else self.roots).append(gid)
        self.loose = []
        for n in graph["nodes"]:
            (self.members[n["group"]] if n.get("group") in self.groups else self.loose).append(n["id"])
        refs = {**graph.get("refs", {}), **self.nodes}
        self.badges = {}
        for e in graph["edges"]:
            if is_badge_edge(e, refs):
                self.badges[e["source"]] = self.badges.get(e["source"], 0) + 1
        self.links = {nid: len(v) for nid, v in graph.get("links", {}).items()}
        self.size, self.rel, self.boxes = {}, {}, {}
        self.title_lines = {}  # group id -> lines of its title (1 or 2)
        self.lines = {}        # group id -> rows of movable units (nodes, frames) that align_rows may slide
        self.units = {}        # group id -> its movable units in drawing order
        self.used_cols = {}    # group id -> columns of its grid

    def kind(self, gid):
        return self.groups[gid].get("kind") or "other"

    def is_wrap(self, gid):
        return gid in self.groups and self.kind(gid) in WRAP_KINDS

    def movable(self, iid):
        return iid in self.nodes or self.is_wrap(iid)

    def pad(self, gid):
        return WRAP_PAD if self.is_wrap(gid) else PAD

    def head(self, gid):
        if self.is_wrap(gid):
            return WRAP_HEAD + (16 if self.groups[gid].get("detail") else 0)
        return HEAD + TITLE_LH * (self.title_lines.get(gid, 1) - 1)

    def contents(self, iid):
        """The item and everything inside it."""
        out = [iid]
        if iid in self.groups:
            for c in self.children[iid]:
                out += self.contents(c)
            out += self.members[iid]
        return out

    def unit_in(self, gid, item):
        """The direct member of `gid` (node or child group) that holds `item`, or None."""
        while item is not None:
            parent = self.nodes[item].get("group") if item in self.nodes else self.groups[item].get("parent") if item in self.groups else None
            if parent == gid:
                return item
            item = parent if parent in self.groups else None
        return None

    def unit_order(self, gid):
        units = [c for c in self.children[gid] if self.is_wrap(c)] + self.members[gid]
        if gid in self.order and sorted(self.order[gid]) == sorted(units):
            return list(self.order[gid])
        edges = []
        for e in self.graph["edges"]:
            a, b = self.unit_in(gid, e["source"]), self.unit_in(gid, e["target"])
            if a and b and a != b:
                edges.append({"source": a, "target": b})
        return flow_order(units, edges)

    def measure_node(self, nid):
        w, h = node_size(self.nodes[nid], self.badges.get(nid, 0), self.links.get(nid, 0))
        refs = self.graph.get("refs", {})
        for link in self.graph.get("links", {}).get(nid, []):  # a chip to another view widens the cell to show the name
            name = str((refs.get(link["far"]) or {}).get("label") or link["far"])
            w = max(w, min(LINK_MAX_W, snap(text_width(name, 9) + text_width(f"{NBSP}· View {link['view']}", 9, True) + 40)))
        self.size[nid] = (w, h)
        return self.size[nid]

    def stretch(self, lines):
        """Groups side by side in a row get the row's height, so a row of zones reads as one band."""
        for line in lines:
            groups = [i for i in line if i in self.groups and not self.is_wrap(i)]
            if len(line) > 1 and groups:
                top = max(self.size[i][1] for i in line)
                lanes = len(groups) == len(line) and len({self.kind(i) for i in groups}) == 1 and not any(self.children[i] for i in groups)
                for i in groups:
                    if lanes or self.size[i][1] >= STRETCH * top:  # a much shorter zone stays short rather than half empty; lanes line up
                        self.size[i] = (self.size[i][0], top)

    def measure_group(self, gid):
        g = self.groups[gid]
        kind = self.kind(gid)
        for cid in self.children[gid]:
            self.measure_group(cid)
        for nid in self.members[gid]:
            self.measure_node(nid)
        wrapped = self.is_wrap(gid)
        if kind == "vpc":
            w, h = self.arrange_vpc(gid)
        elif kind == "region":
            w, h = self.arrange_region(gid)
        else:
            fixed = [c for c in self.children[gid] if not self.is_wrap(c)]
            if kind == "az":
                order = list(self.groups)
                fixed.sort(key=lambda c: subnet_order(self.groups[c], order.index(c)))
            units = self.unit_order(gid)
            items = fixed + units
            cols = 1 if kind == "az" else max(1, self.cols.get(gid) or columns_for(len(items), kind))
            pos, w, h, lines = grid([(i, *self.size[i]) for i in items], cols)
            self.rel.update(pos)
            self.stretch(lines)
            self.units[gid], self.used_cols[gid] = units, cols
            self.lines[gid] = [line for line in lines if all(self.movable(i) for i in line)]
        if not wrapped:  # a long title takes two lines rather than widening the group far beyond its content (a frame's is shortened)
            need = text_width(str(g.get("label", "")), GROUP_LABEL_SIZE, True) + 44
            w = max(w, 120, need if need <= max(w, TITLE_MIN_W) else max(TITLE_MIN_W, need / 2 + 40))
            self.title_lines[gid] = len(group_title(g, snap(w + 2 * PAD))[0])
        self.size[gid] = (snap(w + 2 * self.pad(gid)), snap(h + self.head(gid) + self.pad(gid)))
        return self.size[gid]

    def arrange_region(self, gid):
        kids = self.children[gid]
        vpcs = [c for c in kids if self.kind(c) == "vpc"]
        services = [c for c in kids if self.kind(c) == "services"]
        rest = [c for c in kids if c not in vpcs and c not in services and not self.is_wrap(c)]
        units = self.unit_order(gid)
        items = [(i, *self.size[i]) for i in vpcs + services + rest + units]
        pos, w, h, lines = rows(items, max([MAX_ROW_W] + [i[1] for i in items]))
        self.rel.update(pos)
        self.stretch(lines)
        self.units[gid], self.used_cols[gid] = units, max([len(line) for line in lines] or [1])
        self.lines[gid] = [line for line in lines if all(self.movable(i) for i in line)]
        return w, h

    def equalize_azs(self, azs):
        """Subnets of one tier sit at the same height in every AZ column (public tiers first, then private, matched by
        position), and every AZ column has one width."""
        columns = {}
        for az in azs:
            subs = self.children[az]
            if self.members[az] or any(self.kind(s) != "subnet" for s in subs):
                return
            order = list(self.groups)
            ranked = sorted(subs, key=lambda s: subnet_order(self.groups[s], order.index(s)))  # address, then graph order: not the label's language
            public = [s for s in ranked if is_public_subnet(self.groups[s])]
            private = [s for s in ranked if not is_public_subnet(self.groups[s])]
            columns[az] = [((0, i), s) for i, s in enumerate(public)] + [((1, i), s) for i, s in enumerate(private)]
        if len(columns) < 2:
            return
        keys = sorted({k for col in columns.values() for k, _ in col})
        tier_h = {k: max(self.size[s][1] for col in columns.values() for kk, s in col if kk == k) for k in keys}
        inner = max([self.size[s][0] for col in columns.values() for _, s in col] + [self.size[az][0] - 2 * PAD for az in azs])
        for az in azs:
            have, y = dict(columns[az]), 0
            for k in keys:
                if k in have:
                    self.rel[have[k]] = (0, y)
                    self.size[have[k]] = (inner, tier_h[k])
                y += tier_h[k] + GAP
            self.size[az] = (inner + 2 * PAD, snap(y - GAP + HEAD + PAD))

    def arrange_vpc(self, gid):
        kids = self.children[gid]
        azs = sorted([c for c in kids if self.kind(c) == "az"], key=lambda c: str(self.groups[c].get("az") or self.groups[c].get("label")))
        spans = [c for c in kids if self.kind(c) == "subnet-span"]
        others = [c for c in kids if c not in azs and c not in spans and not self.is_wrap(c)]
        units = self.unit_order(gid)
        gates = [u for u in units if u in self.nodes and node_type(self.nodes[u]) in GATEWAYS]
        rest = [u for u in units if u not in gates]
        self.equalize_azs(azs)
        y, width, lines = 0, 0, []
        if gates:  # the internet and VPN gateways sit on top, next to the public subnets, as in the official diagrams
            pos, gw, gh, gl = rows([(nid, *self.size[nid]) for nid in gates])
            self.rel.update(pos); y = gh + GAP; width = gw; lines += gl
        top = azs + others
        top_lines = []
        if top:
            pos, tw, th, top_lines = rows([(c, *self.size[c]) for c in top], max(MAX_ROW_W, sum(self.size[c][0] + GAP_X for c in azs)))  # the AZ columns stay side by side
            self.rel.update({k: (x, py + y) for k, (x, py) in pos.items()})
            self.stretch(top_lines)
            th = max(self.size[c][1] + self.rel[c][1] - y for c in top)
            y += th + GAP; width = max(width, tw)
        for cid in spans:
            cw, ch = self.size[cid]
            self.rel[cid] = (0, y); y += ch + GAP; width = max(width, cw)
        cols = 1
        if rest:
            items = [(nid, *self.size[nid]) for nid in rest]
            cols = self.cols.get(gid) or (max(1, min(len(items), (max(width, NODE_W) + GAP_X) // (NODE_W + GAP_X))) if width else columns_for(len(items), "other"))
            pos, pw, ph, rl = grid(items, cols)
            for nid, (px, py) in pos.items():
                self.rel[nid] = (px, py + y)
            width = max(width, pw); y += ph + GAP; lines += rl
        width = snap(width)
        # span bands run across the whole VPC; AZ columns widen to fill it
        for cid in spans:
            self.size[cid] = (width, self.size[cid][1])
        if azs and not others and len(top_lines) == 1:
            row_w = sum(self.size[c][0] for c in azs) + GAP_X * (len(azs) - 1)
            add = (width - row_w) // len(azs) // GRID * GRID
            if add > 0:
                x = 0
                for az in azs:
                    w, h = self.size[az]
                    self.size[az] = (w + add, h); self.rel[az] = (x, self.rel[az][1]); x += w + add + GAP_X
                    for s in self.children[az]:
                        self.size[s] = (w + add - 2 * PAD, self.size[s][1])
        self.units[gid], self.used_cols[gid] = units, cols
        self.lines[gid] = [line for line in lines if all(self.movable(i) for i in line)]
        return width, max(y - GAP, 0)

    def place(self, iid, ox, oy):
        x, y = self.rel.get(iid, (0, 0))
        w, h = self.size[iid]
        ax, ay = ox + x, oy + y
        self.boxes[iid] = (ax, ay, w, h)
        if iid in self.groups:
            pad, head = self.pad(iid), self.head(iid)
            for cid in self.children[iid]:
                self.place(cid, ax + pad, ay + head)
            for nid in self.members[iid]:
                self.place(nid, ax + pad, ay + head)

    def run(self):
        for gid in self.roots:
            self.measure_group(gid)
        for nid in self.loose:
            self.measure_node(nid)
        items = [(gid, *self.size[gid]) for gid in self.roots] + [(nid, *self.size[nid]) for nid in self.loose]
        if self.roots:
            pos, w, h, _ = rows(items, max(MAX_ROW_W, max([i[1] for i in items] or [0])))
        else:
            pos, w, h, _ = grid(items, columns_for(len(items), "other"))
        self.rel.update(pos)
        for iid in [i for i, _, _ in items]:
            self.place(iid, 0, 0)
        return self.boxes

    def shift(self, boxes, iid, dx):
        for i in self.contents(iid):
            x, y, w, h = boxes[i]
            boxes[i] = (x + dx, y, w, h)


# ---------------------------------------------------------------------------
# Grouping resources drawn around what they apply to
# ---------------------------------------------------------------------------
WRAPPER_TYPES = {"elbv2.target-group": "target-group", "autoscaling.group": "autoscaling", "eks.nodegroup": "autoscaling",
                 "ec2.security-group": "security-group"}


def member_lines(grouping, ids, kind, say):
    """(title, mono detail) of a frame or container for the grouping resources `ids` (from the graph's `members`):
    the name and the short id or port, so a reader can find the resource in the console."""
    members = {m["id"]: m for m in (grouping or {}).get("members") or []}
    found = [members[i] for i in ids if i in members]
    if not found:
        return None
    if len(found) == 1:
        m = found[0]
        ref = m.get("detail") if kind == "target-group" else short_id(m.get("native_id"))
        return m["name"], ref or ""
    return ", ".join(m["name"] for m in found), ", ".join(short_id(m.get("native_id")) if kind != "target-group" else (m.get("detail") or "") for m in found)


def wrap_groups(graph, say=lambda text: text):
    """The view graph with grouping resources turned into containers. A target group (or Auto Scaling group, or node
    group) whose members all sit in one zone becomes a dashed container around them inside that zone: the container
    carries the resource's node id, connectors to the resource end at its border, and the membership edges are drawn by
    the containment (their ids are on the container). An aggregate of target groups whose members each belong to one
    target group becomes one container per target group, named with its name and port; a connector to the aggregate
    goes to each target group it concerns (the extra lines repeat the edge id as data-edge-repeat). A security group
    attachment becomes a red dashed frame around the attached component, titled with the group's name and short id,
    carrying the attachment edge's id; the security group node itself stays where it is. Anything that cannot be drawn
    this way keeps the plain drawing."""
    nodes = {n["id"]: dict(n) for n in graph["nodes"]}
    known = {**graph.get("refs", {}), **nodes}
    groups = [dict(g) for g in graph.get("groups", [])]
    parent_of = {g["id"]: g.get("parent") for g in groups}
    edges = [dict(e) for e in graph["edges"]]
    links = graph.get("links", {})
    carried, taken = {}, set()

    def kind_of(n):
        return WRAPPER_TYPES.get(node_type(n or {}))

    def ancestry(gid):
        out = set()
        while gid:
            out.add(gid); gid = parent_of.get(gid)
        return out

    def container(gid, kind, w, label, detail, home, k=0):
        groups.append({"id": gid, "kind": kind, "label": label, "detail": detail, "parent": home, "wraps": w["id"],
                       "state": w.get("state") or "observed", "title": " ".join(filter(None, [label, detail]))})
        parent_of[gid] = home
    badge_sources = {e["source"] for e in edges if is_badge_edge(e, known)}
    for wid in [n["id"] for n in graph["nodes"]]:
        w = nodes.get(wid)
        kind = kind_of(w)
        if kind not in {"target-group", "autoscaling"} or links.get(wid) or wid in badge_sources:
            continue
        if kind == "target-group":
            mine = [e for e in edges if e["source"] == wid and e["target"] in nodes and not kind_of(nodes[e["target"]])]
        else:
            mine = [e for e in edges if wid in (e["source"], e["target"]) and node_type(nodes.get(e["target"] if e["source"] == wid else e["source"]) or {}) == "ec2.instance"]
        mine = [e for e in mine if e["id"] not in taken]
        member_of = lambda e: e["target"] if e["source"] == wid else e["source"]  # noqa: E731
        members = list(dict.fromkeys(member_of(e) for e in mine))
        if not members:
            continue
        # One container per target group, when every membership edge names exactly one and each one's members share a zone.
        per = {}
        if kind == "target-group" and w.get("members") and all(len(e.get("source_resources") or []) == 1 for e in mine):
            for e in mine:
                per.setdefault(e["source_resources"][0], []).append(e)
            homes = {t: {nodes[member_of(e)].get("group") for e in es} for t, es in per.items()}
            if set(per) != {m["id"] for m in w["members"]} or any(len(h) != 1 or None in h or (w.get("group") and w["group"] not in ancestry(next(iter(h)))) for h in homes.values()):
                per = {}
        if per:
            order = [m["id"] for m in w["members"] if m["id"] in per]
            ids = {}
            for k, tg in enumerate(order):
                gid = "~wrap:" + wid + (f"~{k}" if k else "")
                label, detail = member_lines(w, [tg], kind, say)
                container(gid, kind, w, label, detail, next(iter({nodes[member_of(e)].get("group") for e in per[tg]})), k)
                for e in per[tg]:
                    nodes[member_of(e)]["group"] = gid
                carried[gid] = per[tg]
                taken |= {e["id"] for e in per[tg]}
                ids[tg] = gid
            del nodes[wid]
            extra = []
            for e in edges:
                if e["id"] in taken or wid not in (e["source"], e["target"]):
                    continue
                end = "target" if e["target"] == wid else "source"
                ends = [ids[t] for t in e.get(f"{end}_resources") or [] if t in ids] or [ids[order[0]]]
                e[end] = ends[0]
                for j, other in enumerate(ends[1:], 1):
                    extra.append(dict(e, id=f"{e['id']}~{j}", label="", repeat_of=e["id"], **{end: other}))
            edges += extra
            continue
        homes = {nodes[m].get("group") for m in members}
        if len(homes) != 1:
            continue
        home = homes.pop()
        if home is None or (w.get("group") and w["group"] not in ancestry(home)):
            continue
        gid = "~wrap:" + wid
        named = member_lines(w, [m["id"] for m in w.get("members") or []], kind, say) if len(w.get("members") or []) == 1 else None
        label, detail = named or (w.get("label", ""), visible_detail(w.get("detail"), w.get("label", "")))
        container(gid, kind, w, label, detail, home)
        for m in members:
            nodes[m]["group"] = gid
        carried[gid] = mine
        taken |= {e["id"] for e in mine}
        del nodes[wid]
        for e in edges:
            if e["id"] not in taken:
                e["source"] = gid if e["source"] == wid else e["source"]
                e["target"] = gid if e["target"] == wid else e["target"]
    framed = {}
    for e in edges:
        if e["id"] not in taken and e["source"] in nodes and kind_of(known.get(e["target"])) == "security-group" and is_badge_edge(e, known):
            framed.setdefault(e["source"], []).append(e)
    for src, mine in framed.items():
        sg = known.get(mine[0]["target"]) or {}
        ids = list(dict.fromkeys(x for e in mine for x in e.get("target_resources") or []))
        named = member_lines(sg, ids, "security-group", say)
        single = len(mine) == 1 and len(sg.get("resource_ids") or []) <= 1 and "×" not in str(sg.get("label", ""))
        label, detail = named or ((sg.get("label") if single and sg.get("label") else say("Security group")), "")
        gid = "~frame:" + src
        groups.append({"id": gid, "kind": "security-group", "label": label, "detail": detail, "parent": nodes[src].get("group"), "frame_for": src,
                       "title": " ".join(filter(None, [say("Security group"), label, detail]))})
        parent_of[gid] = nodes[src].get("group")
        nodes[src]["group"] = gid
        carried[gid] = mine
        taken |= {e["id"] for e in mine}
    return dict(graph, nodes=[nodes[n["id"]] for n in graph["nodes"] if n["id"] in nodes], groups=groups,
                edges=[e for e in edges if e["id"] not in taken], carried=carried)


# ---------------------------------------------------------------------------
# Arrangement search: column counts, member order, rows slid under what they connect to
# ---------------------------------------------------------------------------
def anchor(boxes, iid, node_ids):
    x, y, w, h = boxes[iid]
    return (x + w / 2, y + NODE_TOP + ICON / 2) if iid in node_ids else (x + w / 2, y + h / 2)


def neighbours(edges):
    out = {}
    for e in edges:
        out.setdefault(e["source"], []).append(e["target"]); out.setdefault(e["target"], []).append(e["source"])
    return out


def align_line(lay, boxes, line, x0, x1, nbr, node_ids):
    """Slide one row of nodes (and frames) sideways, keeping their order: each under the mean position of what it
    connects to outside the row, the others centred, GAP_X apart at least, inside [x0, x1]."""
    units = sorted(line, key=lambda u: boxes[u][0])
    packed = sum(boxes[u][2] for u in units) + GAP_X * (len(units) - 1)
    if packed > x1 - x0 + 0.5:
        return
    inside = {u: set(lay.contents(u)) for u in units}
    in_row = set().union(*inside.values())
    desired, x = {}, x0 + (x1 - x0 - packed) / 2 // GRID * GRID
    for u in units:
        others = [o for i in inside[u] for o in nbr.get(i, ()) if o not in in_row and o in boxes]
        if others:
            centre = sum(anchor(boxes, o, node_ids)[0] for o in others) / len(others)
            desired[u] = round((centre - boxes[u][2] / 2) / GRID) * GRID
        else:
            desired[u] = x
        x += boxes[u][2] + GAP_X
    seq = units  # the order is reorder()'s choice; here the row only slides
    pos = [desired[u] for u in seq]
    widths = [boxes[u][2] for u in seq]
    for i in range(len(seq)):
        pos[i] = max(pos[i], x0 if i == 0 else pos[i - 1] + widths[i - 1] + GAP_X)
    for i in reversed(range(len(seq))):
        pos[i] = min(pos[i], x1 - widths[i] if i == len(seq) - 1 else pos[i + 1] - GAP_X - widths[i])
    if pos[0] < x0 - 0.5:
        return
    for u, p in zip(seq, pos):
        if abs(p - boxes[u][0]) > 0.1:
            lay.shift(boxes, u, p - boxes[u][0])


def align_rows(lay, boxes, edges, passes=2):
    node_ids = set(lay.nodes)
    nbr = neighbours(edges)
    order = sorted(lay.groups, key=lambda g: group_depth(lay.groups, g))
    for _ in range(passes):
        for gid in order:
            if gid not in boxes:
                continue
            gx, gy, gw, gh = boxes[gid]
            for line in lay.lines.get(gid, []):
                align_line(lay, boxes, line, gx + lay.pad(gid), gx + gw - lay.pad(gid), nbr, node_ids)


def segments_cross(a, b, c, d):
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def segment_meets_box(a, b, box):
    """True when the straight segment a-b passes through `box` (Liang-Barsky clipping)."""
    x, y, w, h = box
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x), (dx, x + w - a[0]), (-dy, a[1] - y), (dy, y + h - a[1])):
        if abs(p) < 1e-9:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def layout_cost(lay, boxes, edges):
    """A quick estimate of how hard the connectors will be to follow: straight-line length between the ends, crossings
    between those lines, components in their way, and the drawing's size."""
    node_ids = set(lay.nodes)
    lines = [(e["source"], e["target"], anchor(boxes, e["source"], node_ids), anchor(boxes, e["target"], node_ids)) for e in edges]
    length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for _, _, a, b in lines)
    crossings = sum(1 for i in range(len(lines)) for j in range(i + 1, len(lines))
                    if not {lines[i][0], lines[i][1]} & {lines[j][0], lines[j][1]} and segments_cross(lines[i][2], lines[i][3], lines[j][2], lines[j][3]))
    blockers = 0
    inner = {i: set(lay.contents(i)) for s, t, _, _ in lines for i in (s, t) if i in lay.groups}
    for s, t, a, b in lines:
        skip = {s, t} | inner.get(s, set()) | inner.get(t, set())
        for nid in node_ids:
            if nid not in skip:
                x, y, w, h = boxes[nid]
                blockers += segment_meets_box(a, b, (x + 8, y + 8, w - 16, h - 16))
    xs = [b[0] for b in boxes.values()] + [b[0] + b[2] for b in boxes.values()]
    ys = [b[1] for b in boxes.values()] + [b[1] + b[3] for b in boxes.values()]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    return length + 220 * crossings + 160 * blockers + size_cost(width, height)


def size_cost(width, height):
    """What a drawing's size costs its reader: height and area (empty space between groups), more when it is taller than
    4:3 landscape (it scrolls at page width) or wider than SPLIT_W (its text shrinks)."""
    return (height + 0.25 * width + AREA * width * height + ASPECT * max(0, height - 0.75 * width)
            + 20 * max(0, width - SPLIT_W))


def reorder(lay, boxes, edges):
    """Each group's members sorted by the mean position of what they connect to (rows by height, then left to right);
    a member with no connection keeps its place. Ties keep the current order, so the result is deterministic."""
    node_ids = set(lay.nodes)
    nbr = neighbours(edges)
    order = {}
    for gid, units in lay.units.items():
        if len(units) < 2:
            continue
        keyed = []
        for i, u in enumerate(units):
            inside = set(lay.contents(u))
            others = [o for x in inside for o in nbr.get(x, ()) if o not in inside and o in boxes]
            if others:
                pts = [anchor(boxes, o, node_ids) for o in others]
                keyed.append(((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)), i, u))
            else:
                keyed.append((anchor(boxes, u, node_ids), i, u))
        cols = max(1, lay.used_cols.get(gid, len(units)))
        if cols >= len(units):
            new = [u for _, _, u in sorted(keyed, key=lambda k: (k[0][0], k[1]))]
        else:
            by_y = sorted(keyed, key=lambda k: (k[0][1], k[1]))
            new = []
            for r in range(0, len(by_y), cols):
                new += [u for _, _, u in sorted(by_y[r:r + cols], key=lambda k: (k[0][0], k[1]))]
        order[gid] = new
    return order


FINALISTS = 6   # arrangements with the lowest estimate that are routed in full before one is chosen
AREA, ASPECT = 1 / 1000, 4


def arrange(graph, badge_ids, finalists=None):
    """Boxes of the drawing: for every group the column count and member order, found by a deterministic local search
    (barycentre ordering passes, then each group's column options in turn) on a quick estimate (layout_cost), with rows
    slid under what they connect to. The FINALISTS best arrangements are then routed in full and the one whose
    connectors are easiest to follow (route_score) wins; besides the lowest estimates, the best arrangement of every
    column choice for a family of sibling groups (a pipeline's stages as lanes, two wide, ...) is routed too, since an
    estimate cannot see bends. `finalists=0` returns the best estimate. Returns (Layout, boxes)."""
    tried = {}

    def build(cols, order):
        key = json.dumps([sorted(cols.items()), sorted((k, v) for k, v in order.items())])
        if key not in tried:
            lay = Layout(graph, cols, order)
            boxes = lay.run()
            routed = [e for e in graph["edges"] if e["id"] not in badge_ids and e["source"] in boxes and e["target"] in boxes and e["source"] != e["target"]]
            align_rows(lay, boxes, routed)
            tried[key] = (layout_cost(lay, boxes, routed), cols, order, lay, boxes, routed, len(tried))
        return tried[key]
    def polish(start):
        """`start` after barycentre reordering passes, while they lower the estimate."""
        for _ in range(4):
            new = reorder(start[3], start[4], start[5])
            if new == start[2]:
                break
            trial = build(start[1], new)
            if trial[0] >= start[0] - 0.5:
                break
            start = trial
        return start
    best = build({}, {})
    if not best[5]:
        return best[3], best[4]
    for _ in range(2):
        improved = False
        trial = polish(best)
        if trial is not best:
            best, improved = trial, True
        lay = best[3]
        for family in sibling_families(lay):  # stages of a pipeline: all as columns, or all two wide
            for c in (1, 2):
                trial = polish(build(dict(best[1], **{gid: c for gid in family}), best[2]))
                if trial[0] < best[0] - 0.5:
                    best, improved = trial, True
        lay = best[3]
        for gid in [g for g in lay.groups if lay.kind(g) not in {"az", "region"} and gid_items(lay, g) >= 2]:
            current = lay.used_cols.get(gid)
            for c in column_options(gid_items(lay, gid), lay.kind(gid)):
                if c == current:
                    continue
                trial = build(dict(best[1], **{gid: c}), best[2])
                if trial[0] < best[0] - 0.5:
                    best, improved, current = trial, True, c
        if not improved:
            break
    ranked = sorted(tried.values(), key=lambda t: (t[0], t[6]))
    if finalists == 0:
        return ranked[0][3], ranked[0][4]
    chosen, families = ranked[:FINALISTS if finalists is None else finalists], sibling_families(best[3])
    for t in ranked:
        key = [t[3].used_cols.get(g) for family in families for g in family]
        if len(chosen) < FINALISTS + 3 and all([c.used_cols.get(g) for family in families for g in family] != key for c in [x[3] for x in chosen]):
            chosen.append(t)
    scored = [(route_score(graph, t[4], route_edges(graph, t[4], badge_ids)), t[6], t) for t in chosen]
    chosen = min(scored, key=lambda s: (s[0], s[1]))[2]
    return chosen[3], chosen[4]


def route_score(graph, boxes, routes):
    """How hard the routed connectors are to follow, in the units of layout_cost: length, crossings, overlaps, bends,
    connectors no path was found for, and the drawing's size."""
    fallback = len(routes.get("__fallback__", []))
    lines = [pts for eid, (pts, _) in ((k, v) for k, v in routes.items() if k != "__fallback__")]
    length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for pts in lines for a, b in zip(pts, pts[1:]))
    bends = sum(max(0, len(pts) - 2) for pts in lines)
    segs = [[(a, b) for a, b in zip(pts, pts[1:]) if abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.5] for pts in lines]
    crossings = overlaps = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            for a, b in segs[i]:
                for c, d in segs[j]:
                    kind = segment_relation(a, b, c, d)
                    crossings += kind == "cross"; overlaps += kind == "overlap"
    xs = [b[0] for b in boxes.values()] + [b[0] + b[2] for b in boxes.values()]
    ys = [b[1] for b in boxes.values()] + [b[1] + b[3] for b in boxes.values()]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    return length + 220 * crossings + 300 * overlaps + 40 * bends + 5000 * fallback + size_cost(width, height)


def sibling_families(lay):
    """Groups of three or more sibling groups of nodes only (the stages of a pipeline, the tiers of a region)."""
    out = []
    for parent in list(lay.groups) + [None]:
        kids = lay.children[parent] if parent else lay.roots
        family = [k for k in kids if not lay.children[k] and lay.members[k] and lay.kind(k) not in {"az", "subnet"} | WRAP_KINDS]
        if len(family) >= 3:
            out.append(family)
    return out


def gid_items(lay, gid):
    """How many items the group's grid holds (the VPC: its row of VPC-level nodes)."""
    if lay.kind(gid) == "vpc":
        return len([u for u in lay.units.get(gid, []) if not (u in lay.nodes and node_type(lay.nodes[u]) in GATEWAYS)])
    return len([c for c in lay.children[gid] if not lay.is_wrap(c)]) + len(lay.units.get(gid, []))


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------
def group_title(group, width):
    """The lines of a zone's title in a box `width` wide (beside the badge when it has one), two at most."""
    badge = zone_style(group)[1][2]
    return wrap(group.get("label", ""), GROUP_LABEL_SIZE, width - (32 if badge else 8) - 8, 2, True)


def group_headers(graph, boxes):
    """The title band of each group (badge and label), which connectors and edge labels keep clear of."""
    out = []
    for g in graph.get("groups", []):
        if g["id"] not in boxes:
            continue
        x, y, w, h = boxes[g["id"]]
        if (g.get("kind") or "") in WRAP_KINDS:
            badge = 22 if ZONES.get(g.get("kind"), ZONES["other"])[2] else 6
            label_w = text_width(str(g.get("label") or ""), WRAP_LABEL_SIZE, True)
            detail_w = text_width(str(g.get("detail") or ""), DETAIL_SIZE) if g.get("detail") else 0
            out.append((x, y, min(w, badge + max(label_w, detail_w) + 10), 34 if g.get("detail") else 18))
        else:
            lines, _ = group_title(g, w)
            out.append((x, y, min(w, 34 + max([text_width(line, GROUP_LABEL_SIZE, True) for line in lines] or [0]) + 10), 24 + TITLE_LH * (max(1, len(lines)) - 1)))
    return out


TURN = 10       # a bend costs as much as this many cells of length: one-bend routes win over stairs
CROSS = 14      # crossing another connector
OVERLAP = 80    # running on top of another connector
NEAR = 3        # running in the cell right beside another connector
HALO = 2        # running in the cell right beside a component
BORDER = 3      # running along, or crossing, a group border
TITLE = 400     # crossing a group title (only when there is no other way; the QA then fails the render)
INTERIOR = 2    # running through a group that holds neither end (a connector keeps to the corridors between groups)
GREED = 1.4     # the A* heuristic is weighted: a route within a few percent of the cheapest, found much faster
INF = float("inf")
SIDES = {"right": 0, "left": 1, "bottom": 2, "top": 3}
DX, DY, REVERSE = (1, -1, 0, 0), (0, 0, 1, -1), (1, 0, 3, 2)


def route_edges(graph, boxes, badge_ids, cell=GRID):
    """Orthogonal connectors that go around nodes. Edges are routed shortest first; each one is an A* search on a grid
    of `cell`-px cells from every free port of the source (four sides, ports spread along the icon) to every free port of
    the target, so the search picks the sides and ports that need the fewest bends. Bends, crossings, running beside or
    on top of another connector, hugging a component and crossing group borders or titles all cost extra. Connectors to
    a container (a target group drawn around its members) end at its border. Returns {edge id: (points, None)} and
    "__fallback__": the edges no path was found for (drawn straight; the QA fails them)."""
    routes = {"__fallback__": []}
    node_ids = {n["id"] for n in graph["nodes"] if n["id"] in boxes}
    edges = [e for e in graph["edges"] if e["id"] not in badge_ids and e["source"] in boxes and e["target"] in boxes and e["source"] != e["target"]]
    if not edges:
        return routes
    min_x = math.floor((min(b[0] for b in boxes.values()) - 4 * cell) / cell) * cell - cell / 2
    min_y = math.floor((min(b[1] for b in boxes.values()) - 4 * cell) / cell) * cell - cell / 2
    max_x = max(b[0] + b[2] for b in boxes.values()) + 4 * cell
    max_y = max(b[1] + b[3] for b in boxes.values()) + 4 * cell
    cols, nrows = int((max_x - min_x) // cell) + 1, int((max_y - min_y) // cell) + 1
    size = cols * nrows
    blocked, toll = bytearray(size), [0] * size
    used_h, used_v = bytearray(size), bytearray(size)

    def cell_of(x, y):
        return (min(cols - 1, max(0, int((x - min_x) // cell))), min(nrows - 1, max(0, int((y - min_y) // cell))))

    def centre(c):
        return (min_x + (c % cols + 0.5) * cell, min_y + (c // cols + 0.5) * cell)

    def raise_toll(cx, cy, value):
        if 0 <= cx < cols and 0 <= cy < nrows:
            toll[cy * cols + cx] = max(toll[cy * cols + cx], value)
    for g in graph.get("groups", []):
        if g["id"] not in boxes:
            continue
        x, y, w, h = boxes[g["id"]]
        (c0x, c0y), (c1x, c1y) = cell_of(x, y), cell_of(x + w, y + h)
        for cx in range(c0x, c1x + 1):
            raise_toll(cx, c0y, BORDER); raise_toll(cx, c1y, BORDER)
        for cy in range(c0y, c1y + 1):
            raise_toll(c0x, cy, BORDER); raise_toll(c1x, cy, BORDER)
    for nid in node_ids:
        x, y, w, h = boxes[nid]
        (c0x, c0y), (c1x, c1y) = cell_of(x - 3, y - 3), cell_of(x + w + 3, y + h + 3)
        for cy in range(c0y, c1y + 1):
            for cx in range(c0x, c1x + 1):
                blocked[cy * cols + cx] = 1
        for cx in range(c0x - 1, c1x + 2):
            raise_toll(cx, c0y - 1, HALO); raise_toll(cx, c1y + 1, HALO)
        for cy in range(c0y - 1, c1y + 2):
            raise_toll(c0x - 1, cy, HALO); raise_toll(c1x + 1, cy, HALO)
    for x, y, w, h in group_headers(graph, boxes):
        (c0x, c0y), (c1x, c1y) = cell_of(x, y), cell_of(x + w, y + h)
        for cy in range(c0y, c1y + 1):
            for cx in range(c0x, c1x + 1):
                toll[cy * cols + cx] = TITLE
    # the innermost group around each cell (security group frames aside), to keep connectors out of unrelated groups
    groups = {g["id"]: g for g in graph.get("groups", []) if g["id"] in boxes}
    index = {gid: i for i, gid in enumerate(groups)}
    owner = [-1] * size
    for gid in sorted(groups, key=lambda g: group_depth(groups, g)):
        if (groups[gid].get("kind") or "") == "security-group":
            continue
        x, y, w, h = boxes[gid]
        (c0x, c0y), (c1x, c1y) = cell_of(x + 1, y + 1), cell_of(x + w - 1, y + h - 1)
        for cy in range(c0y, c1y + 1):
            owner[cy * cols + c0x: cy * cols + c1x + 1] = [index[gid]] * (c1x - c0x + 1)
    parent_of = {n["id"]: n.get("group") for n in graph["nodes"]}
    parent_of.update({gid: g.get("parent") for gid, g in groups.items()})

    def around(item):
        out, item = set(), item if item in groups else parent_of.get(item)
        while item in groups:
            out.add(index[item]); item = parent_of.get(item)
        return out
    taken = {}  # (item, side) -> slot coordinates in use

    def slots(item, side):
        """(coordinate along the side, cost) of the ports of one side: on a node across the middle of the icon, on a
        container along the middle of its side; the middle first."""
        x, y, w, h = boxes[item]
        if item in node_ids:
            ix, iy, iw, ih = icon_box(boxes[item])
            lo, hi = (iy + 8, iy + ih - 8) if side in {"left", "right"} else (ix + 8, ix + iw - 8)
        else:
            lo, hi = (y + 32, y + h - 12) if side in {"left", "right"} else (x + w * 0.25, x + w * 0.75)
        origin = min_y if side in {"left", "right"} else min_x
        k0, k1 = math.ceil((lo - origin) / cell - 0.5), math.floor((hi - origin) / cell - 0.5)
        mid = (lo + hi) / 2
        values = sorted((origin + (k + 0.5) * cell for k in range(k0, k1 + 1)), key=lambda v: (abs(v - mid), v))
        used = taken.get((item, side), set())
        return [(v, 0.6 * i) for i, v in enumerate(values[:9]) if v not in used]

    def port(item, side, v):
        """(point on the box, point on the icon or None, first free cell outside)."""
        x, y, w, h = boxes[item]
        p = {"left": (x, v), "right": (x + w, v), "top": (v, y), "bottom": (v, y + h)}[side]
        reach = None
        if item in node_ids:
            ix, iy, iw, ih = icon_box(boxes[item])
            reach = {"left": (ix, v), "right": (ix + iw, v), "top": (v, iy), "bottom": None}[side]
        d = SIDES[side]
        cx, cy = cell_of(p[0] + DX[d] * (cell / 2 + 1), p[1] + DY[d] * (cell / 2 + 1))
        for _ in range(16):
            if not blocked[cy * cols + cx]:
                break
            cx, cy = min(cols - 1, max(0, cx + DX[d])), min(nrows - 1, max(0, cy + DY[d]))
        return p, reach, cy * cols + cx

    def search(starts, goals, allowed):
        gxs, gys = [c % cols for c in goals], [c // cols for c in goals]
        gx0, gx1, gy0, gy1 = min(gxs), max(gxs), min(gys), max(gys)

        def h(c):
            cx, cy = c % cols, c // cols
            dx = gx0 - cx if cx < gx0 else cx - gx1 if cx > gx1 else 0
            dy = gy0 - cy if cy < gy0 else cy - gy1 if cy > gy1 else 0
            return (dx + dy + (TURN if dx and dy else 0)) * GREED
        heap, best, parent, finals, origin = [], [INF] * (size * 4), {}, [], {}
        for c, d, g0, info in starts:
            key = c * 4 + d
            if g0 < best[key]:
                best[key], parent[key], origin[key] = g0, None, info
                heapq.heappush(heap, (g0 + h(c), g0, key))
        while heap:
            f, g, key = heapq.heappop(heap)
            if key < 0:
                last, info = finals[-key - 1]
                path = []
                while last is not None:
                    path.append(last); first = last; last = parent[last]
                return [k // 4 for k in reversed(path)], origin[first], info
            if g > best[key]:
                continue
            c, d = divmod(key, 4)
            for inward, info, cost in goals.get(c, ()):
                total = g + cost + (0 if d == inward else TURN)
                finals.append((key, info))
                heapq.heappush(heap, (total, total, -len(finals)))
            cx, cy = c % cols, c // cols
            for nd in range(4):
                if nd == REVERSE[d]:
                    continue
                nx, ny = cx + DX[nd], cy + DY[nd]
                if not (0 <= nx < cols and 0 <= ny < nrows):
                    continue
                nc = ny * cols + nx
                if blocked[nc] and nc not in goals:
                    continue
                step = 1 + toll[nc] + (TURN if nd != d else 0) + (INTERIOR if owner[nc] >= 0 and owner[nc] not in allowed else 0)
                if nd < 2:
                    step += OVERLAP if used_h[nc] else CROSS if used_v[nc] else 0
                    if (ny > 0 and used_h[nc - cols]) or (ny < nrows - 1 and used_h[nc + cols]):
                        step += NEAR
                else:
                    step += OVERLAP if used_v[nc] else CROSS if used_h[nc] else 0
                    if (nx > 0 and used_v[nc - 1]) or (nx < cols - 1 and used_v[nc + 1]):
                        step += NEAR
                ng, nkey = g + step, nc * 4 + nd
                if ng < best[nkey]:
                    best[nkey], parent[nkey] = ng, key
                    heapq.heappush(heap, (ng + h(nc), ng, nkey))
        return None

    def mid(item):
        x, y, w, h = boxes[item]
        return (x + w / 2, y + h / 2)
    edges.sort(key=lambda e: (abs(mid(e["source"])[0] - mid(e["target"])[0]) + abs(mid(e["source"])[1] - mid(e["target"])[1])))
    for e in edges:
        s, t = e["source"], e["target"]
        starts, goals = [], {}
        for side, d in SIDES.items():
            for v, cost in slots(s, side):
                p, reach, c = port(s, side, v)
                starts.append((c, d, cost, (side, v, p, reach)))
            for v, cost in slots(t, side):
                p, reach, c = port(t, side, v)
                goals.setdefault(c, []).append((REVERSE[d], (side, v, p, reach), cost))
        found = search(starts, goals, around(s) | around(t)) if starts and goals else None
        if not found:
            a, b = mid(s), mid(t)
            routes[e["id"]] = ([a, b], None)
            routes["__fallback__"].append(e["id"])
            continue
        cells, (s_side, s_v, a, a_reach), (t_side, t_v, b, b_reach) = found
        for i in range(len(cells) - 1):
            horizontal = abs(cells[i + 1] - cells[i]) == 1
            for c in (cells[i], cells[i + 1]):
                (used_h if horizontal else used_v)[c] = 1
        taken.setdefault((s, s_side), set()).add(s_v)
        taken.setdefault((t, t_side), set()).add(t_v)
        pts = ([a_reach] if a_reach else []) + [a] + [centre(c) for c in cells] + [b] + ([b_reach] if b_reach else [])
        routes[e["id"]] = (simplify(pts), None)
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


LABEL_FORMS = ((170, 2), (124, 3), (84, 3), (72, 4))   # (wrap width, lines): an edge label tries the widest form first


def boxes_meet(a, b, pad=0):
    return a[0] - pad < b[0] + b[2] and b[0] - pad < a[0] + a[2] and a[1] - pad < b[1] + b[3] and b[1] - pad < a[1] + a[3]


def segment_hits(box, a, b):
    """True when the orthogonal segment a-b passes through `box`."""
    x, y, w, h = box
    if abs(a[1] - b[1]) < 0.5:
        return y <= a[1] <= y + h and min(a[0], b[0]) <= x + w and max(a[0], b[0]) >= x
    return x <= a[0] <= x + w and min(a[1], b[1]) <= y + h and max(a[1], b[1]) >= y


def place_labels(graph, routes, boxes, badge_ids, headers, extent, first=1):
    """A position for every edge label that covers no node, no group title and no other label and that no other
    connector crosses, beside its own line (wrapped narrower when that fits a gap). A label that fits nowhere becomes a
    numbered marker on its connector, explained under the legend. Returns ({edge id: placement}, [(number, text)])."""
    node_boxes = [boxes[n["id"]] for n in graph["nodes"] if n["id"] in boxes]
    segments = {eid: list(zip(pts, pts[1:])) for eid, (pts, _) in routes.items()}
    placed, keyed, out = [], [], {}
    x0, y0, x1, y1 = extent

    def free(box):
        if box[0] < x0 or box[1] < y0 or box[0] + box[2] > x1 or box[1] + box[3] > y1:
            return False
        return not any(boxes_meet(box, b, 2) for b in node_boxes) and not any(boxes_meet(box, h, 1) for h in headers) and not any(boxes_meet(box, b, 2) for b in placed)

    def crossed(box, eid):
        return any(segment_hits(box, a, b) for other, segs in segments.items() if other != eid for a, b in segs)
    borders = []  # group outlines: a label sitting on one reads as the group's caption
    for g in graph.get("groups", []):
        if g["id"] in boxes:
            gx, gy, gw, gh = boxes[g["id"]]
            borders += [((gx, gy), (gx + gw, gy)), ((gx, gy + gh), (gx + gw, gy + gh)), ((gx, gy), (gx, gy + gh)), ((gx + gw, gy), (gx + gw, gy + gh))]

    def on_border(box):
        return any(segment_hits(box, a, b) for a, b in borders)
    edges = [e for e in graph["edges"] if e["id"] in routes and e["id"] not in badge_ids and " ".join(str(e.get("label") or "").split())]
    for e in sorted(edges, key=lambda e: (-len(str(e.get("label"))), e["id"])):
        text = " ".join(str(e["label"]).split())
        segs = sorted(segments[e["id"]], key=lambda ab: -(abs(ab[0][0] - ab[1][0]) + abs(ab[0][1] - ab[1][1])))
        longest_word = max(text_width(word, DETAIL_SIZE) for word in text.split(" "))
        forms = []
        for width, max_lines in LABEL_FORMS:
            lines, cut = wrap(text, DETAIL_SIZE, width, max_lines)
            if not cut and width >= longest_word:  # a narrower form may not cut a word
                forms.append((lines, snap(max(text_width(line, DETAIL_SIZE) for line in lines) + 8, 2), len(lines) * EDGE_LABEL_LH + 5))

        def find(strict):
            for lines, w, h in forms:
                for a, b in segs:
                    horizontal = abs(a[1] - b[1]) < 0.5
                    if abs(a[0] - b[0]) + abs(a[1] - b[1]) < (24 if horizontal else 16):
                        continue
                    for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
                        px, py = a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac
                        for bx, by in ([(px - w / 2, py - h - 6), (px - w / 2, py + 6)] if horizontal else [(px + 8, py - h / 2), (px - 8 - w, py - h / 2)]):
                            box = (bx, by, w, h)
                            if free(box) and not crossed(box, e["id"]) and not (strict and on_border(box)):
                                return {"lines": lines, "box": box}
            return None
        spot = find(True) or find(False)
        if spot:  # beside its own line and crossed by no other: anything else reads as another line's label
            placed.append(spot["box"])
            out[e["id"]] = spot
            continue
        number = first + len(keyed)
        centre = None
        for a, b in segs:
            for frac in (0.5, 0.3, 0.7, 0.15, 0.85):
                cx, cy = a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac
                if free((cx - 8, cy - 8, 16, 16)):
                    centre = (cx, cy); break
            if centre:
                break
        if centre is None:
            a, b = segs[0]
            centre = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        placed.append((centre[0] - 8, centre[1] - 8, 16, 16))
        keyed.append((number, text))
        out[e["id"]] = {"marker": number, "center": centre}
    return out, keyed


def points_to_path(pts, radius=8, hops=None):
    """An orthogonal polyline with rounded bends (quadratic corners of `radius`, smaller on short runs) and a small
    bridge (a half circle of HOP) where `hops` says it crosses a more important connector: {segment index: [distance
    from the segment's start]}."""
    if len(pts) < 2:
        return ""
    hops = hops or {}
    n = len(pts)
    length = lambda a, b: abs(b[0] - a[0]) + abs(b[1] - a[1])  # noqa: E731
    rad = [0.0] * n
    for i in range(1, n - 1):
        rad[i] = min(radius, length(pts[i - 1], pts[i]) / 2, length(pts[i], pts[i + 1]) / 2)
    out = [f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"]
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        seg = length(a, b)
        ux, uy = ((b[0] - a[0]) / seg, (b[1] - a[1]) / seg) if seg else (0, 0)
        sweep = 1 if ux > 0 or uy > 0 else 0
        for t in sorted(hops.get(i, [])):
            if t - HOP < rad[i] + 4 or t + HOP > seg - rad[i + 1] - 4:
                continue
            h0, h1 = (a[0] + ux * (t - HOP), a[1] + uy * (t - HOP)), (a[0] + ux * (t + HOP), a[1] + uy * (t + HOP))
            out.append(f"L {h0[0]:.1f} {h0[1]:.1f} A {HOP} {HOP} 0 0 {sweep} {h1[0]:.1f} {h1[1]:.1f}")
        end = (b[0] - ux * rad[i + 1], b[1] - uy * rad[i + 1])
        out.append(f"L {end[0]:.1f} {end[1]:.1f}")
        if i + 1 < n - 1:
            c = pts[i + 2]
            nl = length(b, c)
            vx, vy = ((c[0] - b[0]) / nl, (c[1] - b[1]) / nl) if nl else (0, 0)
            out.append(f"Q {b[0]:.1f} {b[1]:.1f} {b[0] + vx * rad[i + 1]:.1f} {b[1] + vy * rad[i + 1]:.1f}")
    return " ".join(out)


EDGE_RANK = {"observed_configuration": 3, "observed": 3, "proposed": 3, "declared": 2, "unresolved": 1}


def crossing_hops(graph, routes, badge_edges):
    """Where each connector bridges another: at every crossing of a horizontal and a vertical run, the less important
    connector (declared before unconfirmed; among equals the later one) makes the hop, as aws-diagram-design asks.
    Returns {edge id: {segment index: [distance from the segment's start]}}."""
    order = [e for e in graph["edges"] if e["id"] in routes and e not in badge_edges]
    rank = {e["id"]: (EDGE_RANK.get(e.get("state") or "observed_configuration", 3), -i) for i, e in enumerate(order)}
    segs = {e["id"]: list(zip(routes[e["id"]][0], routes[e["id"]][0][1:])) for e in order}
    out = {}
    ids = [e["id"] for e in order]
    for i, ea in enumerate(ids):
        for eb in ids[i + 1:]:
            for ia, (a0, a1) in enumerate(segs[ea]):
                for ib, (b0, b1) in enumerate(segs[eb]):
                    ha, hb = abs(a0[1] - a1[1]) < 0.5, abs(b0[1] - b1[1]) < 0.5
                    if ha == hb:
                        continue
                    (h0, h1, ih, eh), (v0, v1, iv, ev) = ((a0, a1, ia, ea), (b0, b1, ib, eb)) if ha else ((b0, b1, ib, eb), (a0, a1, ia, ea))
                    x, y = v0[0], h0[1]
                    if not (min(h0[0], h1[0]) + 2 < x < max(h0[0], h1[0]) - 2 and min(v0[1], v1[1]) + 2 < y < max(v0[1], v1[1]) - 2):
                        continue
                    hopper, seg, start = (eh, ih, h0) if rank[eh] < rank[ev] else (ev, iv, v0)
                    out.setdefault(hopper, {}).setdefault(seg, []).append(abs(x - start[0]) + abs(y - start[1]))
    return out


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
    while groups[gid].get("parent") in groups:
        gid = groups[gid]["parent"]; depth += 1
    return depth


def renderer_words():
    """The renderer's own English words (legend lines, state tags, the legend heading), for a drawing in another
    language: the localizer translates these with the graph's labels and redraws."""
    out = ["Legend", "Connections", "Architecture", "View", LINK_LEGEND, CHIP_LEGEND]
    out += list(STATE_TAGS.values())
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
        if (item.get("kind") or "") == "subnet" and "public" not in item:
            item["public"] = is_public_subnet(item)  # decided on the English label, before it is translated
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
SPLIT_W, SPLIT_H = 1500, 1800
SPLIT_CROSS = 0.3          # ... unless every cut leaves more than this share of the connectors running between views
PART_GAP, PART_HEAD = 48, 32
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
    wrapped = wrap_groups(graph)
    trial = arrange(wrapped, {e["id"] for e in wrapped["edges"] if is_badge_edge(e, {**wrapped.get("refs", {}), **{x["id"]: x for x in wrapped["nodes"]}})}, 0)[1]
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
        return None  # the parts are too entangled: two views would turn most connectors into chips
    views = [level[bounds[v]:bounds[v + 1]] for v in range(len(bounds) - 1)]
    return parent, views, {nid: view_of[i] for nid, i in owner.items()}, trees


def view_graph(graph, parent, sections, trees, node_view, index, badge_ids, say):
    """The part of the graph one view draws. A connector to a component of another view is not a line: it is a chip
    under the component of this view ("→ Secrets Manager · View 2"), and the edge keeps its id on the first chip drawn."""
    groups = {g["id"]: g for g in graph.get("groups", [])}
    nodes = {n["id"]: n for n in graph["nodes"]}
    keep, gid = set(), parent
    while gid:
        keep.add(gid); gid = groups[gid].get("parent") if groups[gid].get("parent") in groups else None
    for section in sections:
        keep |= trees[section]
    mine = [n for n in graph["nodes"] if node_view.get(n["id"], 0) == index]
    ids = {n["id"] for n in mine}
    edges, refs, links = [], {}, {}
    for e in graph["edges"]:
        s_in, t_in = e["source"] in ids, e["target"] in ids
        if e["id"] in badge_ids:
            if s_in:
                edges.append(dict(e, drawn_as="badge")); refs[e["target"]] = nodes.get(e["target"]) or {}
            continue
        if s_in and t_in:
            edges.append(e)
        elif s_in or t_in:
            here, far = (e["source"], e["target"]) if s_in else (e["target"], e["source"])
            refs[far] = nodes.get(far) or {}
            links.setdefault(here, []).append({"edge": e, "dir": "out" if s_in else "in", "far": far, "view": node_view.get(far, 0) + 1})
    labels = [str(groups[c].get("label") or "") for c in sections]
    return {"title": graph.get("title"), "groups": [g for g in graph.get("groups", []) if g["id"] in keep], "nodes": mine, "edges": edges,
            "refs": refs, "links": links, "heading": f"{say('View')} {index + 1}: " + " · ".join(l for l in labels if l)}


def layout_view(graph, first=1, say=None):
    """Geometry of one drawing: grouping resources turned into containers, node and group boxes, routed connectors,
    edge labels (numbered from `first`)."""
    graph = wrap_groups(graph, say or (lambda text: text))
    nodes_by_id = {**graph.get("refs", {}), **{n["id"]: n for n in graph["nodes"]}}
    badge_edges = [e for e in graph["edges"] if is_badge_edge(e, nodes_by_id)]
    badge_ids = {e["id"] for e in badge_edges}
    lay, boxes = arrange(graph, badge_ids)
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
            "straight": straight, "extent": extent, "headers": headers, "labels": labels, "keyed": keyed, "layout": lay}


def edge_title(e, nodes):
    label = " ".join(str(e.get("label") or "").split())
    ends = f'{(nodes.get(e["source"]) or {}).get("label") or e["source"]} → {(nodes.get(e["target"]) or {}).get("label") or e["target"]}'
    return f"{label}: {ends}" if label else ends


def draw_view(view, dx, dy, icon_set, say, notes, drawn=None, all_nodes=None):
    """SVG fragments of one laid-out drawing, shifted by (dx, dy): groups, connectors, nodes, then edge labels on top.
    `drawn` holds the ids an earlier view already carries: a repeat (the region around every view, the second chip of a
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
    names = {**(all_nodes or {}), **nodes_by_id, **{gid: {"label": g.get("title") or g.get("label")} for gid, g in groups.items() if gid.startswith("~")}}
    carried = graph.get("carried", {})
    out, top = [], []
    for gid in sorted(groups, key=lambda g: (group_depth(groups, g), list(groups).index(g))):
        g = groups[gid]
        x, y, w, h = boxes[gid]
        x += dx; y += dy
        kind, (color, dash, badge, legend) = zone_style(g)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        if kind in WRAP_KINDS:
            opener = "".join(f'<g {ident("edge", e["id"])} data-drawn-as="{esc(kind)}"><title>{esc(edge_title(e, names))}</title>' for e in carried.get(gid, []))
            state = g.get("state") or "observed"
            if g.get("wraps"):
                marker = f'{ident("node", g["wraps"])} data-state="{esc(state)}" data-kind="{esc(kind)}"'
            else:
                marker = f'data-frame-for="{esc(g.get("frame_for") or "")}" data-kind="{esc(kind)}"'
            title = g.get("title") or g.get("label") or ""
            out.append(f'{opener}<g {marker}><title>{esc(title)}</title><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="4" fill="none" stroke="{color}" stroke-width="1"{dash_attr}/>')
            label_x = x + 6
            icon = icon_set.inner(f"group/{badge}") if badge else None
            if icon:
                body, bw, _ = icon
                out.append(f'<g transform="translate({x:.1f},{y:.1f}) scale({18 / bw:.4f})" aria-hidden="true">{body}</g>')
                label_x = x + 24
            line = fit_line(g.get("label", ""), WRAP_LABEL_SIZE, x + w - label_x - 6, True)
            if line:
                out.append(f'<text x="{label_x:.1f}" y="{y + 13:.1f}" font-size="{WRAP_LABEL_SIZE}" font-weight="600" fill="{color}">{esc(line)}</text>')
            if g.get("detail"):
                out.append(f'<text x="{label_x:.1f}" y="{y + 26:.1f}" font-size="{ID_SIZE}" fill="{TEXT_2}" font-family="{MONO}">{esc(fit_line(g["detail"], ID_SIZE * 1.12, x + w - label_x - 6))}</text>')
            if state in STATE_TAGS:
                s_color = STATES.get(state, STATES["observed"])[0]
                tag_text = say(STATE_TAGS[state])
                tw = text_width(tag_text, 8, True) + 10
                out.append(f'<rect x="{x + w - tw - 6:.1f}" y="{y - 7:.1f}" width="{tw:.1f}" height="14" rx="3" fill="{s_color}"/>'
                           f'<text x="{x + w - tw / 2 - 6:.1f}" y="{y + 3.3:.1f}" font-size="8" font-weight="700" fill="{PAPER}" text-anchor="middle">{esc(tag_text)}</text>')
            out.append("</g>" + "</g>" * len(carried.get(gid, [])))
            continue
        fill = FILLS.get(kind)
        fill_attr = f' fill="{fill}"' if fill else ' fill="none"'
        out.append(f'<g {ident("group", gid)} data-kind="{esc(kind)}"><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" stroke="{color}" stroke-width="1"{dash_attr}{fill_attr}/>')
        label_x = x + 8
        icon = icon_set.inner(f"group/{badge}") if badge else None
        if icon:
            body, bw, bh = icon
            out.append(f'<g transform="translate({x:.1f},{y:.1f}) scale({24 / bw:.4f})" aria-hidden="true">{body}</g>')
            label_x = x + 32
        elif badge:
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="24" height="24" fill="{color}"/>')
            label_x = x + 32
        lines, truncated = group_title(g, w)
        if truncated:
            notes.append(f"group {gid}: label shortened to fit {w:.0f}px")
        for k, line in enumerate(lines):
            out.append(f'<text x="{label_x:.1f}" y="{y + 16.5 + k * TITLE_LH:.1f}" font-size="{GROUP_LABEL_SIZE}" font-weight="600" fill="{INK}">{esc(line)}</text>')
        out.append("</g>")
    out.append(f'<g transform="translate({dx:.1f},{dy:.1f})" fill="none">')
    edge_states, missing_routes = set(), []
    hops = crossing_hops(graph, routes, badge_edges)
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
        out.append(f'<g {ident("edge", e.get("repeat_of") or e["id"])}><title>{esc(edge_title(e, names))}</title><path d="{points_to_path(pts, hops=hops.get(e["id"]))}" stroke="{color}" stroke-width="1.2"{dash_attr} stroke-linejoin="round" marker-end="url(#@@SLUG@@-{marker})"/></g>')
    out.append("</g>")
    top.append(f'<g transform="translate({dx:.1f},{dy:.1f})">')
    for eid, spot in labels.items():
        if "lines" in spot:
            bx, by, bw, bh = spot["box"]
            top.append(f'<g data-label-for="{esc(eid)}"><rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="{PAPER}"/>')
            for k, line in enumerate(spot["lines"]):
                top.append(f'<text x="{bx + bw / 2:.1f}" y="{by + 11 + k * EDGE_LABEL_LH:.1f}" font-size="{DETAIL_SIZE}" fill="{TEXT_2}" text-anchor="middle">{esc(line)}</text>')
            top.append("</g>")
        else:
            cx, cy = spot["center"]
            top.append(f'<g data-label-for="{esc(eid)}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="8" fill="{INK}" stroke="{PAPER}" stroke-width="1.5"/>'
                       f'<text x="{cx:.1f}" y="{cy + 3.5:.1f}" font-size="9" font-weight="700" fill="{PAPER}" text-anchor="middle" font-family="{MONO}">{spot["marker"]}</text></g>')
    top.append("</g>")
    states = set()
    links = graph.get("links", {})
    for n in graph["nodes"]:
        x, y, w, h = boxes[n["id"]]
        x += dx; y += dy
        state = n.get("state") or "observed"
        color, dash, sw, _ = STATES.get(state, STATES["observed"])
        states.add(state if state in STATES else "observed")
        label, detail, truncated = node_lines(n)
        if truncated:
            notes.append(f"node {n['id']}: text shortened to {MAX_LABEL_LINES}+{MAX_DETAIL_LINES} lines")
        title_text = " ".join(filter(None, [n.get("label"), n.get("detail")]))
        out.append(f'<g data-node-id="{esc(n["id"])}" data-state="{esc(state)}"><title>{esc(title_text)}</title>')
        if state not in PLAIN_STATES | TAG_ONLY:
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" fill="none" stroke="{color}" stroke-width="{sw}"{dash_attr}/>')
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
        rid = id_line(n)
        if rid:
            out.append(f'<text x="{cx_text:.1f}" y="{ty + 1:.1f}" font-size="{ID_SIZE}" fill="{SOFT}" text-anchor="middle" font-family="{MONO}">{esc(rid)}</text>'); ty += ID_LH
        if state in STATE_TAGS:
            tag_text = say(STATE_TAGS[state])
            tw = text_width(tag_text, 8, True) + 10
            out.append(f'<rect x="{x + w - tw - 6:.1f}" y="{y - 7:.1f}" width="{tw:.1f}" height="14" rx="3" fill="{color}"/>'
                       f'<text x="{x + w - tw / 2 - 6:.1f}" y="{y + 3.3:.1f}" font-size="8" font-weight="700" fill="{PAPER}" text-anchor="middle">{esc(tag_text)}</text>')
        mine = [e for e in badge_edges if e["source"] == n["id"]]
        my_links = links.get(n["id"], [])
        chips = []
        for e in mine:
            target = nodes_by_id.get(e["target"]) or {}
            _, chip_color, chip_tag = icon_for(target)
            chips.append((e, target, chip_color, chip_tag, text_width(chip_tag, 8, True) + 10))
        chip_y = y + h - LINK_ROW * len(my_links) - (BADGE_ROW - 2)
        cx = x + (w - (sum(c[4] for c in chips) + 4 * max(0, len(chips) - 1))) / 2
        for e, target, chip_color, chip_tag, cw in chips:
            out.append(f'<g {ident("edge", e["id"])}><title>{esc(edge_title(e, names))}</title>'
                       f'<rect x="{cx:.1f}" y="{chip_y:.1f}" width="{cw:.1f}" height="11" rx="2" fill="{chip_color}" fill-opacity="0.12" stroke="{chip_color}" stroke-width="0.8"/>'
                       f'<text x="{cx + cw / 2:.1f}" y="{chip_y + 8.2:.1f}" font-size="7.5" font-weight="700" fill="{chip_color}" text-anchor="middle" font-family="{MONO}">{esc(chip_tag)}</text></g>')
            cx += cw + 4
        for k, link in enumerate(my_links):
            out.append(link_chip(link, x + 4, y + h - LINK_ROW * (len(my_links) - k) + 2, w - 8, names, say, ident))
            edge_states.add(link["edge"].get("state") if link["edge"].get("state") in EDGE_STATES else "observed_configuration")
        out.append("</g>")
    kinds = [zone_style(groups[gid]) for gid in groups]
    return out, top, states, edge_states, kinds, missing_routes


def link_chip(link, x, y, w, names, say, ident):
    """A connector to a component of another view, as a pill under the component: the direction ("→" to it, "←" from
    it), the other component's name (shortened to fit) and its view. The full text is in the <title>."""
    e, far = link["edge"], names.get(link["far"]) or {}
    color, dash, _ = EDGE_STATES.get(e.get("state") or "observed_configuration", EDGE_STATES["observed_configuration"])
    arrow = "→" if link["dir"] == "out" else "←"
    where = f"{NBSP}· {say('View')} {link['view']}"
    name = fit_line(far.get("label") or link["far"], 9, w - 30 - text_width(where, 9, True))
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<g {ident("edge", e["id"])} data-view-link="{link["view"]}"><title>{esc(edge_title(e, names))} ({esc(say("View"))} {link["view"]})</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="19" rx="9.5" fill="{PAPER}" stroke="{color}" stroke-width="1"{dash_attr}/>'
            f'<text x="{x + 11:.1f}" y="{y + 13.5:.1f}" font-size="11" font-weight="700" fill="{color}" text-anchor="middle">{arrow}</text>'
            f'<text x="{x + 21:.1f}" y="{y + 13:.1f}" font-size="9" fill="{INK}">{esc(name)}<tspan fill="{TEXT_2}" font-weight="600">{esc(where)}</tspan></text></g>')


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


def readability(views, width, height):
    """How easy the connectors are to follow: proper crossings between connectors, collinear overlaps, bends, total and
    longest connector length (the longest as a share of the drawing's diagonal), the drawing size, and how many
    connectors became numbered markers or chips to another view."""
    crossings = overlaps = bends = count = 0
    total = longest = 0.0
    for view in views:
        segs = []
        for eid, (pts, _) in view["routes"].items():
            if eid in view.get("badge_ids", ()):
                continue
            count += 1
            bends += max(0, len(pts) - 2)
            length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
            total += length; longest = max(longest, length)
            segs.append([(a, b) for a, b in zip(pts, pts[1:]) if abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.5])
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                for a, b in segs[i]:
                    for c, d in segs[j]:
                        kind = segment_relation(a, b, c, d)
                        crossings += kind == "cross"; overlaps += kind == "overlap"
    diagonal = (width ** 2 + height ** 2) ** 0.5 or 1
    return {"connectors": count, "crossings": crossings, "overlaps": overlaps, "bends": bends,
            "bends_per_connector": round(bends / count, 2) if count else 0, "length": round(total),
            "longest_share_of_diagonal": round(longest / diagonal, 3), "width": round(width), "height": round(height),
            "numbered_labels": sum(len(v.get("keyed", [])) for v in views),
            "view_links": sum(len(v["graph"].get("links", {}).get(n, [])) for v in views for n in v["graph"].get("links", {}))}


def segment_relation(a, b, c, d, tol=0.5):
    """'cross' when two orthogonal segments cross inside both, 'overlap' when they run on the same line for more than a
    pixel, else None. Touching at an end (two connectors leaving one port) is neither."""
    ah, ch = abs(a[1] - b[1]) < tol, abs(c[1] - d[1]) < tol
    if ah != ch:
        (h1, h2), (v1, v2) = ((a, b), (c, d)) if ah else ((c, d), (a, b))
        x, y = v1[0], h1[1]
        return "cross" if (min(h1[0], h2[0]) + tol < x < max(h1[0], h2[0]) - tol and min(v1[1], v2[1]) + tol < y < max(v1[1], v2[1]) - tol) else None
    if ah and abs(a[1] - c[1]) < tol:
        lo, hi = max(min(a[0], b[0]), min(c[0], d[0])), min(max(a[0], b[0]), max(c[0], d[0]))
        return "overlap" if hi - lo > 1 else None
    if not ah and abs(a[0] - c[0]) < tol:
        lo, hi = max(min(a[1], b[1]), min(c[1], d[1])), min(max(a[1], b[1]), max(c[1], d[1]))
        return "overlap" if hi - lo > 1 else None
    return None


def legend_block(items, x, y, width):
    """Legend entries (swatch builder, text) in as many columns as fit `width`, filled column by column."""
    if not items:
        return [], 0
    col_w = max(text_width(text, 10) for _, text in items) + 32 + 28
    ncols = max(1, min(3, len(items), int((width + 28) // col_w)))
    nrows = -(-len(items) // ncols)
    out = []
    for i, (swatch, text) in enumerate(items):
        cx, cy = x + (i // nrows) * col_w, y + (i % nrows) * 16
        out.append(swatch(cx, cy))
        out.append(f'<text x="{cx + 30:.1f}" y="{cy:.1f}" font-size="10" fill="{TEXT_2}">{esc(text)}</text>')
    return out, nrows * 16


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
        view = layout_view(g, first, say)
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
            out.append(f'<g data-view="{i + 1}"><text x="{MARGIN}" y="{y + 14:.1f}" font-size="14" font-weight="700" fill="{INK}">{esc(view["graph"]["heading"])}</text>'
                       f'<line x1="{MARGIN}" y1="{y + 22:.1f}" x2="{width - MARGIN:.1f}" y2="{y + 22:.1f}" stroke="#D5DBDB" stroke-width="1"/></g>')
            y += PART_HEAD
        o, t, st, es, kinds, mr = draw_view(view, MARGIN - x0, y - y0, icon_set, say, qa["notes"], drawn, nodes_by_id)
        out += o; top += t; states |= st; edge_states |= es; missing_routes += mr; keyed += view["keyed"]
        for kind, style in kinds:
            if kind not in [k for k, _ in kinds_seen]:
                kinds_seen.append((kind, style))
        y += (y1 - y0) + (PART_GAP if i < len(views) - 1 else 0)
    # Legend, in columns
    items = []
    for s in sorted(states, key=lambda s: list(STATES).index(s)):
        color, dash, sw, text = STATES[s]
        if s in PLAIN_STATES:
            items.append((lambda lx, ly: f'<rect x="{lx + 5}" y="{ly - 10:.1f}" width="12" height="12" rx="2" fill="#879196"/>', say(text)))
        elif s in TAG_ONLY:
            items.append((lambda lx, ly, c=color, t=say(STATE_TAGS[s]): f'<rect x="{lx}" y="{ly - 10:.1f}" width="22" height="12" rx="3" fill="{c}"/>', say(text)))
        else:
            items.append((lambda lx, ly, c=color, d=dash, w=sw: f'<rect x="{lx}" y="{ly - 9:.1f}" width="22" height="12" rx="3" fill="{PAPER}" stroke="{c}" stroke-width="{w}"'
                          + (f' stroke-dasharray="{d}"' if d else "") + "/>", say(text)))
    for s in sorted(edge_states, key=lambda s: list(EDGE_STATES).index(s)):
        color, dash, text = EDGE_STATES[s]
        marker = "arrow-accent" if color == ACCENT else "arrow-soft" if color == SOFT else "arrow"
        items.append((lambda lx, ly, c=color, d=dash, m=marker: f'<line x1="{lx}" y1="{ly - 3:.1f}" x2="{lx + 22}" y2="{ly - 3:.1f}" stroke="{c}" stroke-width="1.2"'
                      + (f' stroke-dasharray="{d}"' if d else "") + f' marker-end="url(#@@SLUG@@-{m})"/>', say(text)))
    for kind, (color, dash, badge, text) in kinds_seen:
        fill = FILLS.get(kind)
        items.append((lambda lx, ly, c=color, d=dash, f=fill: f'<rect x="{lx}" y="{ly - 9:.1f}" width="22" height="12" fill="{f or "none"}" stroke="{c}" stroke-width="1.2"'
                      + (f' stroke-dasharray="{d}"' if d else "") + "/>", say(text)))
    if any(v["badge_edges"] for v in views):
        items.append((lambda lx, ly: f'<rect x="{lx}" y="{ly - 9:.1f}" width="22" height="11" rx="2" fill="{MUTED}" fill-opacity="0.12" stroke="{MUTED}" stroke-width="0.8"/>'
                      f'<text x="{lx + 11}" y="{ly - 0.8:.1f}" font-size="7.5" font-weight="700" fill="{MUTED}" text-anchor="middle" font-family="{MONO}">RT</text>', say(CHIP_LEGEND)))
    if any(v["graph"].get("links") for v in views):
        items.append((lambda lx, ly: f'<rect x="{lx}" y="{ly - 10:.1f}" width="22" height="13" rx="6.5" fill="{PAPER}" stroke="{MUTED}" stroke-width="1"/>'
                      f'<text x="{lx + 11}" y="{ly:.1f}" font-size="9" font-weight="700" fill="{MUTED}" text-anchor="middle">→</text>', say(LINK_LEGEND)))
    legend = [f'<line x1="{MARGIN}" y1="{y + 14:.1f}" x2="{width - MARGIN:.1f}" y2="{y + 14:.1f}" stroke="{RULE}" stroke-width="0.8"/>',
              f'<text x="{MARGIN}" y="{y + 30:.1f}" font-size="9" font-weight="700" fill="{MUTED}" font-family="{MONO}" letter-spacing="1.6">{esc(say("Legend").upper())}</text>']
    block, block_h = legend_block(items, MARGIN, y + 46, width - 2 * MARGIN)
    legend += block
    y_cursor = y + 46 + block_h
    if keyed:
        y_cursor += 10
        legend.append(f'<text x="{MARGIN}" y="{y_cursor:.1f}" font-size="9" font-weight="700" fill="{MUTED}" font-family="{MONO}" letter-spacing="1.6">{esc(say("Connections").upper())}</text>')
        y_cursor += 16
        for number, text in keyed:
            lines, _ = wrap(text, 10, max(400, width - 2 * MARGIN - 40), 3)
            legend.append(f'<circle cx="{MARGIN + 8}" cy="{y_cursor - 3.5:.1f}" r="7" fill="{INK}"/><text x="{MARGIN + 8}" y="{y_cursor - 0.5:.1f}" font-size="8.5" font-weight="700" fill="{PAPER}" text-anchor="middle" font-family="{MONO}">{number}</text>')
            for k, line in enumerate(lines):
                legend.append(f'<text x="{MARGIN + 22}" y="{y_cursor + k * 13:.1f}" font-size="10" fill="{TEXT_2}">{esc(line)}</text>')
            y_cursor += 13 * len(lines) + 4
    if note:
        legend.append(f'<text x="{MARGIN}" y="{y_cursor + 6:.1f}" font-size="10" fill="{MUTED}">{esc(say(note))}</text>')
        y_cursor += 18
    height = y_cursor + MARGIN
    heading = say(title or graph.get("title") or "Architecture")
    subtitle = graph.get("subtitle") or ""
    head = (f'<text x="{MARGIN}" y="34" font-size="20" font-weight="700" fill="{INK}">{esc(heading)}</text>'
            + (f'<text x="{MARGIN}" y="52" font-size="11" fill="{MUTED}">{esc(subtitle)}</text>' if subtitle else ""))
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") + "-" + hashlib.sha1(heading.encode("utf-8")).hexdigest()[:6]
    defs = ('<defs>' + "".join(
        f'<marker id="{slug}-{mid}" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse">'
        f'<polygon points="0 0, 8 3, 0 6" fill="{fill}"/></marker>' for mid, fill in (("arrow", MUTED), ("arrow-accent", ACCENT), ("arrow-soft", SOFT))) + '</defs>')
    body = f'{head}{"".join(out)}{"".join(top)}{"".join(legend)}'.replace("@@SLUG@@", slug)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" lang="{esc(language)}" aria-labelledby="{slug}-title {slug}-desc" data-renderer="aws_diagram" data-views="{len(views)}">'
           f'<title id="{slug}-title">{esc(heading)}</title><desc id="{slug}-desc">{esc(subtitle or note or heading)}</desc>{defs}'
           f'<rect width="100%" height="100%" fill="{PAPER}"/><g font-family="{SANS}">{body}</g></svg>\n')
    # Geometry QA per view: nodes inside their group, no node overlap, connectors end at their nodes and never run through
    # another node, labels clear of nodes, titles and each other.
    merged = {}
    for view in views:
        for key, value in view_checks(view).items():
            merged.setdefault(key, []).extend(value)
    qa["checks"] = {"nodes_inside_their_group": {"status": "pass" if not merged["outside"] else "fail", "outside": merged["outside"]},
                    "labels_clear": {"status": "pass" if not (merged["covered"] or merged["clashing"]) else "fail", "over_nodes_or_titles": merged["covered"],
                                     "over_each_other": merged["clashing"], "numbered": [n for n, _ in keyed]},
                    "group_titles_clear": {"status": "pass" if not merged["through_titles"] else "fail", "crossed_by": sorted(set(merged["through_titles"]))},
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
    if (merged["outside"] or merged["overlaps"] or merged["loose"] or missing_routes or merged["straight"] or merged["covered"] or merged["clashing"]
            or merged["through_nodes"] or merged["through_titles"]):
        qa["status"] = "fail"
    qa["readability"] = readability(views, width, height)
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
