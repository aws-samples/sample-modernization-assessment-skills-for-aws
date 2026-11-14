"""Account posture, AWS-computed recommendations and lifecycle rules.

Evaluated by review.py after the baseline and workload rules. Three kinds of input:
  - account-level inventory records collected by collect.py (level == "account"): Security Hub, GuardDuty, Config,
    Inspector, IAM Access Analyzer, Compute Optimizer, Trusted Advisor (support API), AWS Health, Organizations,
    and the discovery summary from the Resource Groups Tagging API;
  - workload records that carry a lifecycle signal in their own attributes (instance types, Lambda runtimes, SSM
    platform names, Classic Load Balancers, launch configurations, certificates, CloudFront settings);
  - supplementary evidence with support dates read from the API (describe-db-major-engine-versions,
    describe-cluster-versions) and stack drift status.

A "not enabled" answer from an API is a definitive observation (the account has not turned the service on or has
no eligible support plan) and is judged; a denied or failed call stays UNKNOWN. Account-wide services are never
FAIL: a service that is off is a WARN carrying an `enablement` state (enabled, partial, not_enabled, not_in_plan,
unknown, observed) and a recommendation written as a suggestion: what the service does, what enabling it would add
for this workload, and when leaving it off is a reasonable choice. Dates are compared with today's date at review
time; table-driven dates quote the table's verified_on date so the reader can judge staleness.
"""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
TABLES = json.loads((HERE.parent / "assets/lifecycle-tables.json").read_text(encoding="utf-8"))
SH = "https://docs.aws.amazon.com/securityhub/latest/userguide/"
WA = "https://docs.aws.amazon.com/wellarchitected/latest/"
TA_PILLAR = {"security": "Security", "cost_optimizing": "Cost Optimization", "fault_tolerance": "Reliability",
             "performance": "Performance Efficiency", "service_limits": "Reliability", "operational_excellence": "Operational Excellence"}
SUPPORT_PLAN_QUESTION = ("Which AWS Support plan does this account have? The Trusted Advisor and Health APIs answered "
                         "'subscription required', which means Basic or Developer. Business or higher unlocks Trusted Advisor's "
                         "cost, security, fault-tolerance and limit checks and Health notifications of planned changes and "
                         "end-of-support events.")


def today():
    return datetime.now(timezone.utc).date()


def parse_date(value):
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def days_until(value, now):
    when = parse_date(value)
    return None if when is None else (when - now).days


def family(instance_type):
    return str(instance_type or "").split(".")[0]


def ok(resource):
    return (resource.get("collection") or {}).get("status", "ok") == "ok"


def not_enabled(resource):
    return (resource.get("collection") or {}).get("status") == "not_enabled"


def obs(resource, label):
    item = resource["observations"].get(label) or {}
    return item.get("data") if item.get("status") == "ok" else None


def evaluate_platform(emit, resources, evidence, scoped, now=None):
    """emit(rule, pillar, resource, status, observation, recommendation, url, weight, severity, evidence, target, **extra)."""
    now = now or today()
    typed = lambda kind: [r for r in resources if r["type"] == kind]  # noqa: E731
    in_scope = lambda r: r["id"] in scoped  # noqa: E731
    table_date = TABLES.get("verified_on", "unknown")

    # ---- account posture ---------------------------------------------------------------------------------------
    # Account-wide services are judged on their enablement state and never as FAIL: whether Security Hub, Config or
    # Inspector is worth turning on depends on the workload's requirements, compliance obligations and the tooling the
    # team already runs. A service that is off is a WARN with an `enablement` state and a recommendation that explains
    # what it does, what enabling it would add for this workload (with the workload's own resource counts) and when
    # leaving it off is reasonable.
    def count(kind):
        return len([r for r in typed(kind) if in_scope(r)])

    def listing(pairs):
        parts = [f"{n} {label if n == 1 else (label[:-1] + 'ies' if label.endswith('y') else label + 's')}" for n, label in pairs if n]
        if not parts:
            return ""
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]

    workload = {"ec2": count("ec2.instance"), "ecr": count("ecr.repository"), "lambda": count("lambda.function"),
                "eks": count("eks.cluster"), "ecs": count("ecs.cluster"), "rds": count("rds.cluster") + count("rds.instance"),
                "s3": count("s3.bucket"), "kms": count("kms.alias"), "sqs": count("sqs.queue"), "secrets": count("secretsmanager.secret")}
    guardduty_on = any(ok(d) and (obs(d, f"detector:{i}") or {}).get("Status") == "ENABLED"
                       for d in typed("guardduty.detector") for i in d["attributes"].get("DetectorIds") or [])
    iac = next((d for d in typed("discovery.tagged-resources") if ok(d) and (d["attributes"].get("total") or 0)), None)
    iac_share = round(100 * (iac["attributes"].get("cloudformation_managed") or 0) / iac["attributes"]["total"]) if iac else None

    for hub in typed("securityhub.hub"):
        target = f"Security Hub · {hub['region']}"
        if ok(hub):
            standards = (obs(hub, "standards") or {}).get("StandardsSubscriptions") or []
            names = [str(s.get("StandardsArn", "")).rsplit("/", 2)[-2] if "/" in str(s.get("StandardsArn", "")) else s.get("StandardsArn") for s in standards]
            if standards:
                emit("SECURITYHUB_ENABLED", "Security", hub, "PASS",
                     f"Security Hub is enabled in {hub['region']} with {len(standards)} standard(s): {', '.join(str(n) for n in names[:4])}.",
                     "Keep the standard enabled and review its failed controls regularly; this assessment does not read individual Security Hub findings.",
                     SH + "securityhub-standards.html", weight=3, severity="MEDIUM", target=target, enablement="enabled")
            else:
                emit("SECURITYHUB_ENABLED", "Security", hub, "WARN",
                     f"Security Hub is enabled in {hub['region']} but no security standard is subscribed, so it aggregates findings without evaluating controls.",
                     "If the team wants control evaluation, subscribe the AWS Foundational Security Best Practices standard (it needs the Config recorder); "
                     "if Security Hub is used only as a findings inbox, that is a deliberate choice and can stay as it is.",
                     SH + "securityhub-standards.html", weight=3, severity="MEDIUM", target=target, enablement="partial")
        elif not_enabled(hub):
            collects = "the GuardDuty findings already produced here" if guardduty_on else "the findings of GuardDuty, Inspector and Config once they are enabled"
            emit("SECURITYHUB_ENABLED", "Security", hub, "WARN",
                 f"Security Hub is not enabled in {hub['region']}; findings from other security services are not aggregated and no security standard is evaluated continuously.",
                 f"Worth considering when several teams share the account or a compliance standard (CIS, PCI DSS, NIST) has to be shown: Security Hub would bring {collects} "
                 "into one place and run this report's one-time checks (load balancer TLS, ECR, EKS and Secrets Manager controls) continuously. A single team that reviews "
                 "GuardDuty findings directly can defer it; it can be enabled later without any change to the workload.",
                 SH + "securityhub-enable.html", weight=3, severity="MEDIUM", target=target, enablement="not_enabled")
        else:
            emit("SECURITYHUB_ENABLED", "Security", hub, "UNKNOWN", f"Security Hub state could not be read ({(hub.get('collection') or {}).get('error_code')}).",
                 "Grant securityhub:DescribeHub and securityhub:GetEnabledStandards, or confirm with the account owner.", SH + "securityhub-enable.html", target=target, enablement="unknown")

    for det in typed("guardduty.detector"):
        target = f"GuardDuty · {det['region']}"
        ids = det["attributes"].get("DetectorIds") or []
        plans = listing([(workload["eks"], "EKS cluster"), (workload["ecs"], "ECS cluster"), (workload["ec2"], "EC2 instance"),
                         (workload["rds"], "database"), (workload["s3"], "S3 bucket"), (workload["lambda"], "Lambda function")])
        plans_text = (f"For this workload the protection plans that matter cover its {plans} (EKS audit logs and runtime monitoring, RDS login activity, S3 data events, Lambda network activity)."
                      if plans else "For this workload the base analysis of CloudTrail, VPC flow and DNS logs is what applies.")
        if ok(det) and (not ids or not any((obs(det, f"detector:{i}") or {}).get("Status") == "ENABLED" for i in ids)):
            state = "No GuardDuty detector exists" if not ids else "A GuardDuty detector exists but is disabled"
            emit("GUARDDUTY_ENABLED", "Security", det, "WARN", f"{state} in {det['region']}; CloudTrail events, VPC flow and DNS logs are not analysed for threats.",
                 "GuardDuty is usually the first security service worth turning on: it reads logs AWS already keeps, needs no agent or change to the workload, and produces "
                 f"findings (credential misuse, unusual API calls, crypto-mining, known bad hosts) within minutes. {plans_text} If a third-party detection tool already "
                 "covers these sources, note it here; otherwise consider enabling it in every region the workload uses, organization-wide where possible.",
                 "https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_settingup.html", weight=3, severity="HIGH", target=target, enablement="not_enabled")
        elif ok(det):
            details = [obs(det, f"detector:{i}") for i in ids]
            features = sorted({f.get("Name") for d in details if d for f in d.get("Features", []) if f.get("Status") == "ENABLED"})
            emit("GUARDDUTY_ENABLED", "Security", det, "PASS",
                 f"GuardDuty detector enabled in {det['region']}" + (f"; protections on: {', '.join(features[:6])}." if features else "."),
                 f"Keep GuardDuty enabled and review which protection plans the workload needs. {plans_text}",
                 "https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_settingup.html", weight=3, severity="HIGH", target=target, enablement="enabled")
        else:
            emit("GUARDDUTY_ENABLED", "Security", det, "UNKNOWN", f"GuardDuty state could not be read ({(det.get('collection') or {}).get('error_code')}).",
                 "Grant guardduty:ListDetectors and guardduty:GetDetector, or confirm with the account owner.", "https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_settingup.html",
                 target=target, enablement="unknown")

    for rec in typed("config.recorder"):
        target = f"AWS Config · {rec['region']}"
        recorders = rec["attributes"].get("ConfigurationRecorders") or []
        iac_text = (f" {iac_share}% of the tagged resources here carry CloudFormation stack tags; the other {100 - iac_share}% were created outside CloudFormation (by hand or by another tool)."
                    if iac_share is not None else "")
        if ok(rec) and not recorders:
            emit("CONFIG_RECORDER_ENABLED", "Operational Excellence", rec, "WARN",
                 f"No AWS Config recorder in {rec['region']}; the configuration history of resources and their drift from the stacks are not recorded.",
                 "AWS Config keeps a timeline of each resource's configuration, evaluates rules against it and supplies the data Security Hub standards read. It matters most "
                 f"where resources change outside CloudFormation.{iac_text} If the team works from stack templates with drift detection and the account is small, the recorder "
                 "can wait; if an audit trail of changes or Security Hub standards are wanted, consider recording the resource types this workload uses (Config is priced per "
                 "configuration item, so recording every type in a busy account adds cost).",
                 SH + "config-controls.html#config-1", weight=2, severity="MEDIUM", target=target, enablement="not_enabled")
        elif ok(rec):
            status = (obs(rec, "status") or {}).get("ConfigurationRecordersStatus") or []
            recording = any(s.get("recording") for s in status)
            rules = obs(rec, "rules") or []
            if recording:
                emit("CONFIG_RECORDER_ENABLED", "Operational Excellence", rec, "PASS",
                     f"Config recorder is recording in {rec['region']}; {len(rules)} Config rule(s) defined.",
                     "Keep recording on; Config rules or conformance packs for the controls this report flags would catch regressions automatically.",
                     SH + "config-controls.html#config-1", weight=2, severity="MEDIUM", target=target, enablement="enabled")
            else:
                emit("CONFIG_RECORDER_ENABLED", "Operational Excellence", rec, "WARN",
                     f"Config recorder exists in {rec['region']} but is stopped; {len(rules)} Config rule(s) defined.",
                     "If Config is still wanted, start the recorder (a stopped recorder records nothing and its rules evaluate stale data); if it was stopped on purpose to "
                     "save cost, remove the rules too so the state is unambiguous.",
                     SH + "config-controls.html#config-1", weight=2, severity="MEDIUM", target=target, enablement="partial")
        else:
            emit("CONFIG_RECORDER_ENABLED", "Operational Excellence", rec, "UNKNOWN", f"AWS Config state could not be read ({(rec.get('collection') or {}).get('error_code')}).",
                 "Grant config:Describe* or confirm with the account owner.", SH + "config-controls.html#config-1", target=target, enablement="unknown")

    for ins in typed("inspector2.account-status"):
        target = f"Amazon Inspector · {ins['region']}"
        if ok(ins):
            accounts = ins["attributes"].get("accounts") or []
            state = ((accounts[0] if accounts else {}).get("resourceState") or {})
            on = sorted(k for k, v in state.items() if isinstance(v, dict) and v.get("status") == "ENABLED")
            off = sorted(k for k, v in state.items() if isinstance(v, dict) and v.get("status") not in ("ENABLED", None))
            covered = listing([(workload["ec2"], "EC2 instance"), (workload["ecr"], "ECR repository"), (workload["lambda"], "Lambda function")])
            covered_text = f"This workload has {covered} that it would cover." if covered else "This workload has no EC2 instance, ECR repository or Lambda function in scope, so there is little for it to scan today."
            if {"ec2", "ecr"} <= set(on):
                emit("INSPECTOR_ENABLED", "Security", ins, "PASS", f"Inspector scanning on: {', '.join(on)}; off: {', '.join(off) or 'nothing'} ({ins['region']}).",
                     f"Keep the EC2 and ECR scans on and review the critical findings with the patching owner. {covered_text}",
                     "https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html", weight=2, severity="MEDIUM", target=target, enablement="enabled")
            elif on:
                emit("INSPECTOR_ENABLED", "Security", ins, "WARN", f"Inspector scanning on: {', '.join(on)}; off: {', '.join(off) or 'nothing'} ({ins['region']}).",
                     f"Whether to extend scanning depends on what runs where: {covered_text[0].lower() + covered_text[1:]} Enabling a missing type is a one-click account "
                     "setting; leaving it off is reasonable when those resources are covered by another scanner or do not exist here.",
                     "https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html", weight=2, severity="MEDIUM", target=target, enablement="partial")
            else:
                emit("INSPECTOR_ENABLED", "Security", ins, "WARN", f"Inspector scanning is off for every resource type in {ins['region']} ({', '.join(off) or 'none reported'}).",
                     "Amazon Inspector scans EC2 instances (through the SSM agent), container images in ECR and Lambda function packages for known vulnerabilities (CVEs) and "
                     f"lists the packages to patch in priority order. {covered_text} If images are already scanned in the CI pipeline or by another scanner and instances are "
                     "patched on a schedule, Inspector may add little; otherwise the ECR and EC2 scans are a low-effort way to see where patching is needed.",
                     "https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html", weight=2, severity="MEDIUM", target=target, enablement="not_enabled")
        else:
            emit("INSPECTOR_ENABLED", "Security", ins, "UNKNOWN", f"Inspector state could not be read ({(ins.get('collection') or {}).get('error_code')}).",
                 "Grant inspector2:BatchGetAccountStatus or confirm with the account owner.", "https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html",
                 target=target, enablement="unknown")

    for ana in typed("accessanalyzer.analyzer"):
        target = f"IAM Access Analyzer · {ana['region']}"
        if not ok(ana):
            emit("IAM_ACCESS_ANALYZER", "Security", ana, "UNKNOWN", f"Access Analyzer state could not be read ({(ana.get('collection') or {}).get('error_code')}).",
                 "Grant access-analyzer:ListAnalyzers or confirm with the account owner.", "https://docs.aws.amazon.com/IAM/latest/UserGuide/what-is-access-analyzer.html",
                 target=target, enablement="unknown")
            continue
        active = [a for a in ana["attributes"].get("analyzers") or [] if a.get("status") == "ACTIVE"]
        if not active:
            reviewed = listing([(workload["s3"], "S3 bucket"), (workload["kms"], "KMS key"), (workload["sqs"], "SQS queue"), (workload["secrets"], "secret")])
            emit("IAM_ACCESS_ANALYZER", "Security", ana, "WARN", f"No active IAM Access Analyzer in {ana['region']}; resources whose policies allow access from outside the account or organization are not reported.",
                 "IAM Access Analyzer lists S3 buckets, IAM roles, KMS keys, SQS queues, Secrets Manager secrets and other resources that principals outside the account can "
                 "reach, so intended sharing can be confirmed and the rest closed. An external-access analyzer is free and one per region is enough. "
                 + (f"For this workload it would review its {reviewed} and the roles they trust; " if reviewed else "")
                 + "worth creating in any account that shares data with other companies or other accounts.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-getting-started.html", weight=2, severity="MEDIUM", target=target, enablement="not_enabled")
            continue
        findings = [f for a in active for f in (obs(ana, f"findings:{a.get('name')}") or [])]
        by_type = {}
        for f in findings:
            by_type[f.get("resourceType")] = by_type.get(f.get("resourceType"), 0) + 1
        emit("IAM_ACCESS_ANALYZER", "Security", ana, "WARN" if findings else "PASS",
             f"{len(active)} active analyzer(s); {len(findings)} active external-access finding(s)" + (": " + ", ".join(f"{k} × {v}" for k, v in sorted(by_type.items())[:6]) if findings else "."),
             "Review each active finding: archive intended sharing with an archive rule, remove the rest.",
             "https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-findings.html", weight=2, severity="MEDIUM", target=target, enablement="enabled")

    instances = {r["native_id"]: r for r in typed("ec2.instance")}
    for co in typed("computeoptimizer.enrollment"):
        target = f"Compute Optimizer · {co['region']}"
        if not ok(co):
            emit("COMPUTE_OPTIMIZER_ENROLLMENT", "Cost Optimization", co, "UNKNOWN", f"Compute Optimizer enrollment could not be read ({(co.get('collection') or {}).get('error_code')}).",
                 "Grant compute-optimizer:GetEnrollmentStatus or confirm with the account owner.", "https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html",
                 target=target, enablement="unknown")
            continue
        status = co["attributes"].get("status")
        emit("COMPUTE_OPTIMIZER_ENROLLMENT", "Cost Optimization", co, "PASS" if status == "Active" else "WARN",
             f"Compute Optimizer enrollment: {status or 'unknown'}." + ("" if status == "Active" else " Right-sizing and Graviton recommendations are not being computed for this account."),
             "Compute Optimizer is free and read-only: it watches CloudWatch utilization and suggests instance types, Graviton candidates, EBS volume types and Lambda memory "
             "sizes. Opting in a few weeks before a right-sizing exercise gives it the data it needs; there is little reason to leave it off."
             if status != "Active" else "Keep the enrollment; its recommendations appear below for the instances in scope.",
             "https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html", weight=1, severity="LOW", target=target,
             enablement="enabled" if status == "Active" else "not_enabled")
        for rec in obs(co, "ec2_recommendations") or []:
            instance_id = str(rec.get("instanceArn", "")).rsplit("/", 1)[-1]
            inst = instances.get(instance_id)
            if not inst or not in_scope(inst):
                continue
            finding = rec.get("finding")
            options = sorted(rec.get("recommendationOptions") or [], key=lambda o: o.get("rank") or 9)
            best = options[0].get("instanceType") if options else None
            graviton = any(re.match(r"^[a-z]+\d+g[a-z]*\.", str(o.get("instanceType", ""))) for o in options)
            emit("COMPUTE_OPTIMIZER_FINDING", "Cost Optimization", inst, "PASS" if finding == "OPTIMIZED" else ("WARN" if finding else "UNKNOWN"),
                 f"{instance_id} ({rec.get('currentInstanceType')}): Compute Optimizer finding {finding or 'none'}" +
                 (f"; top option {best}" if best and finding != "OPTIMIZED" else "") + ("; a Graviton option is offered" if graviton else "") + ".",
                 "Validate the recommended type against the workload's peak and architecture (x86 vs Graviton), then change it in the template.",
                 "https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-ec2-recommendations.html", weight=2, severity="MEDIUM",
                 evidence=inst["evidence_ids"] + co["evidence_ids"], related_pillars=["Performance Efficiency"])

    for ta in typed("support.trusted-advisor"):
        target = "Trusted Advisor"
        plan = ta["attributes"].get("support_plan")
        if plan == "basic_or_developer":
            emit("TRUSTED_ADVISOR_ACCESS", "Operational Excellence", ta, "UNKNOWN",
                 "The support API answered 'subscription required': Trusted Advisor checks are not available on this account's support plan.",
                 SUPPORT_PLAN_QUESTION, "https://aws.amazon.com/premiumsupport/technology/trusted-advisor/", target=target, question=SUPPORT_PLAN_QUESTION,
                 enablement="not_in_plan")
        elif ok(ta):
            checks = {c["id"]: c for c in ta["attributes"].get("checks") or []}
            summaries = [s for label, item in ta["observations"].items() if label.startswith("summaries:") and item.get("status") == "ok"
                         for s in (item.get("data") or {}).get("summaries", [])]
            flagged = [s for s in summaries if s.get("status") in ("error", "warning")]
            emit("TRUSTED_ADVISOR_ACCESS", "Operational Excellence", ta, "PASS",
                 f"Trusted Advisor available ({len(checks)} checks); {len(flagged)} check(s) flag resources.",
                 "Review the flagged checks below with the account owner; Trusted Advisor runs continuously, so fix the cause rather than the symptom.",
                 "https://aws.amazon.com/premiumsupport/technology/trusted-advisor/", weight=1, severity="LOW", target=target, enablement="enabled")
            flagged.sort(key=lambda s: (0 if s["status"] == "error" else 1, -int((s.get("resourcesSummary") or {}).get("resourcesFlagged") or 0)))
            for s in flagged[:30]:
                check = checks.get(s.get("checkId"), {})
                pillar = TA_PILLAR.get(str(check.get("category")), "Operational Excellence")
                count = (s.get("resourcesSummary") or {}).get("resourcesFlagged")
                emit("TRUSTED_ADVISOR_FLAGGED", pillar, ta, "WARN",
                     f"Trusted Advisor '{check.get('name', s.get('checkId'))}' is {s['status']} with {count} flagged resource(s).",
                     "Open the check in the Trusted Advisor console for the resource list and apply its recommended action.",
                     "https://docs.aws.amazon.com/awssupport/latest/user/trusted-advisor-check-reference.html",
                     weight=2, severity="HIGH" if s["status"] == "error" else "MEDIUM", target=f"Trusted Advisor · {check.get('name', s.get('checkId'))}")
        else:
            emit("TRUSTED_ADVISOR_ACCESS", "Operational Excellence", ta, "UNKNOWN", f"Trusted Advisor could not be read ({(ta.get('collection') or {}).get('error_code')}).",
                 "Grant support:DescribeTrustedAdvisorChecks or confirm the support plan with the account owner.", "https://aws.amazon.com/premiumsupport/technology/trusted-advisor/",
                 target=target, enablement="unknown")

    for he in typed("health.events"):
        target = "AWS Health"
        if not_enabled(he):
            emit("HEALTH_OPEN_EVENTS", "Reliability", he, "UNKNOWN", "The Health API answered 'subscription required'; planned changes and end-of-support notices were not read.",
                 SUPPORT_PLAN_QUESTION, "https://docs.aws.amazon.com/health/latest/ug/what-is-aws-health.html", target=target, question=SUPPORT_PLAN_QUESTION,
                 enablement="not_in_plan")
        elif ok(he):
            events = [e for e in he["attributes"].get("events") or [] if e.get("statusCode") in ("open", "upcoming")]
            emit("HEALTH_OPEN_EVENTS", "Reliability", he, "WARN" if events else "PASS",
                 f"{len(events)} open or upcoming AWS Health event(s)" + (": " + ", ".join(f"{e.get('service')} {e.get('eventTypeCode')}" for e in events[:4]) if events else "."),
                 "Read each event in the Health Dashboard; scheduled changes and end-of-support notices set deadlines for this roadmap.",
                 "https://docs.aws.amazon.com/health/latest/ug/what-is-aws-health.html", weight=2, severity="MEDIUM", target=target, enablement="enabled")
        else:
            emit("HEALTH_OPEN_EVENTS", "Reliability", he, "UNKNOWN", f"AWS Health could not be read ({(he.get('collection') or {}).get('error_code')}).",
                 "Grant health:DescribeEvents or confirm the support plan with the account owner.", "https://docs.aws.amazon.com/health/latest/ug/what-is-aws-health.html",
                 target=target, enablement="unknown")

    for org in typed("organizations.organization"):
        target = "AWS Organizations"
        if not_enabled(org):
            emit("ORGANIZATIONS_MEMBERSHIP", "Operational Excellence", org, "WARN", "This account is not part of an AWS Organization; guardrails, consolidated billing and central security services are per account.",
                 "AWS Organizations groups accounts under one management account with consolidated billing, service control policies and delegated administration of security "
                 "services. A single account that stays single does not need it; if a second environment (test, another team, another region's workload) is planned, setting "
                 "up Organizations or Control Tower before that account exists is far cheaper than moving accounts later.",
                 WA + "framework/sec_securely_operate_multi_accounts.html", weight=1, severity="LOW", target=target, enablement="not_enabled")
        elif ok(org):
            o = org["attributes"].get("Organization") or {}
            zones = (obs(org, "landing_zones") or {}).get("landingZones")
            emit("ORGANIZATIONS_MEMBERSHIP", "Operational Excellence", org, "PASS",
                 f"Member of organization {o.get('Id')} (feature set {o.get('FeatureSet')})" + ("; a Control Tower landing zone is visible." if zones else "."),
                 "Keep security services delegated to the organization's security account and SCPs reviewed.", WA + "framework/sec_securely_operate_multi_accounts.html", weight=1, severity="LOW",
                 target=target, enablement="enabled")
        else:
            emit("ORGANIZATIONS_MEMBERSHIP", "Operational Excellence", org, "UNKNOWN", f"Organization membership could not be read ({(org.get('collection') or {}).get('error_code')}).",
                 "Confirm with the account owner whether the account belongs to an organization.", WA + "framework/sec_securely_operate_multi_accounts.html", target=target, enablement="unknown")

    for bill in typed("ce.cost-by-service"):
        target = "Cost Explorer · account"
        months = (bill["attributes"].get("months") or []) if ok(bill) else []
        if ok(bill) and months:
            top = (months[-1].get("services") or [])[:3]
            # No percentage: the bill covers every workload in the account, so a growth rate would be read as this workload's.
            emit("COST_EXPLORER_VISIBILITY", "Cost Optimization", bill, "PASS",
                 f"Account bill (unblended, USD) for the last {len(months)} closed months: " + ", ".join(f"{m.get('start', '')[:7]} {m.get('total_usd', 0):,.0f}" for m in months)
                 + ". Largest services last month: " + ", ".join(f"{s['service']} {s['usd']:,.0f}" for s in top) + ". Account-wide figures that include other workloads: cost allocation tags are a payer-account setting, so the workload's share is not separated.",
                 "Read the trend with the workload owner: a service that grows month over month without a matching change in traffic is the first place to look. "
                 "For a per-workload view, activate cost allocation tags (aws:cloudformation:stack-name or a project tag) in the payer account; they start attributing cost from activation onward.",
                 "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html", weight=1, severity="LOW", target=target, enablement="enabled")
        elif not_enabled(bill) or (ok(bill) and not months):
            emit("COST_EXPLORER_VISIBILITY", "Cost Optimization", bill, "WARN", "Cost Explorer returned no billing data for this account (not enabled yet, or no closed month available).",
                 "AWS Cost Explorer is free to enable and shows the bill by service, region and tag with month-over-month trends; the first data appears about 24 hours after enabling. "
                 "It is the quickest way to answer which service is driving a rising bill, and this report cannot say so without it.",
                 "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-enable.html", weight=1, severity="LOW", target=target, enablement="not_enabled")
        else:
            emit("COST_EXPLORER_VISIBILITY", "Cost Optimization", bill, "UNKNOWN", f"Cost Explorer could not be read ({(bill.get('collection') or {}).get('error_code')}).",
                 "Grant ce:GetCostAndUsage (part of ReadOnlyAccess) or share the Cost Explorer view with the assessing architect.",
                 "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html", target=target, enablement="unknown")

    org_member = any(ok(o) for o in typed("organizations.organization"))
    for summary in typed("iam.account-summary"):
        values = summary["attributes"].get("SummaryMap") or summary["attributes"]
        mfa, keys = values.get("AccountMFAEnabled"), values.get("AccountAccessKeysPresent")
        if keys is not None:
            emit("ROOT_ACCESS_KEYS", "Security", summary, "FAIL" if keys else "PASS",
                 "The root user has access keys." if keys else "The root user has no access keys.",
                 "Delete the root user's access keys; nothing should call AWS APIs as root, and an IAM role or IAM Identity Center user covers every task that needs keys."
                 if keys else "Keep it that way; create keys for roles or IAM Identity Center users instead.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_root-user_manage_add-key.html", weight=3, severity="HIGH", target="Root user", enablement="enabled" if not keys else "not_enabled")
        if mfa is None:
            continue
        if mfa:
            emit("ROOT_MFA", "Security", summary, "PASS", "MFA is enabled for the root user.", "Keep the MFA device somewhere two people can reach; register a second device.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/enable-mfa-for-root.html", weight=3, severity="HIGH", target="Root user", enablement="enabled")
        elif org_member:
            # A member account whose root credentials were removed through centralized root access has no password, so no MFA.
            emit("ROOT_MFA", "Security", summary, "WARN", "The account summary reports no MFA on the root user (AccountMFAEnabled=0). The account belongs to an organization, "
                 "so its root credentials may have been removed centrally, in which case no root password exists and MFA does not apply.",
                 "Confirm with the organization's administrators whether centralized root access manages this account. If it does not, enable MFA on the root user "
                 "(a hardware key or passkey), store it where two people can reach it, and stop using root for daily work.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_root-user.html#id_root-user-access-management", weight=3, severity="HIGH", target="Root user",
                 question="Does the organization manage this account's root credentials centrally (IAM centralized root access)? If not, who holds the root MFA device?",
                 enablement="not_enabled")
        else:
            emit("ROOT_MFA", "Security", summary, "FAIL", "The root user has no MFA (AccountMFAEnabled=0) in a standalone account; a leaked root password gives full control of the account.",
                 "Enable MFA on the root user (a hardware key or passkey), store it where two people can reach it, and stop using root for daily work.",
                 "https://docs.aws.amazon.com/IAM/latest/UserGuide/enable-mfa-for-root.html", weight=3, severity="HIGH", target="Root user", enablement="not_enabled")

    for disc in typed("discovery.tagged-resources"):
        a = disc["attributes"]
        total, managed = a.get("total") or 0, a.get("cloudformation_managed") or 0
        if not ok(disc) or not total:
            emit("IAC_COVERAGE", "Operational Excellence", disc, "UNKNOWN", f"Tagged-resource discovery returned nothing usable in {disc['region']} ({(disc.get('collection') or {}).get('error_code') or 'empty'}).",
                 "Grant tag:GetResources, or confirm how resources are provisioned.", WA + "framework/ops_dev_integ_auto_mgmt.html", target=f"IaC coverage · {disc['region']}", enablement="unknown")
            continue
        share = round(100 * managed / total)
        emit("IAC_COVERAGE", "Operational Excellence", disc, "PASS" if share >= 80 else "WARN",
             f"{managed} of {total} tagged resources in {disc['region']} carry CloudFormation stack tags ({share}%). Never-tagged resources are not counted, so manually created resources can be undercounted.",
             "Bring the remaining resources under infrastructure as code (import into CloudFormation/CDK or Terraform) so every change goes through review and drift detection.",
             WA + "framework/ops_dev_integ_auto_mgmt.html", weight=2, severity="MEDIUM" if share < 50 else "LOW", target=f"IaC coverage · {disc['region']}", enablement="observed")

    # ---- workload lifecycle signals -----------------------------------------------------------------------------
    flow_calls = [c for c in evidence.calls.values() if c.get("service") == "ec2" and c.get("operation") == "describe-flow-logs"]
    flow_logs = typed("ec2.flow-log")
    for vpc in [v for v in typed("ec2.vpc") if in_scope(v)]:
        region_calls = [c for c in flow_calls if c.get("region") == vpc["region"]]
        if not region_calls or not any(c["status"] == "ok" for c in region_calls):
            continue
        active = [f for f in flow_logs if f["attributes"].get("ResourceId") == vpc["native_id"] and f["attributes"].get("FlowLogStatus") == "ACTIVE"]
        emit("VPC_FLOW_LOGS", "Security", vpc, "PASS" if active else "WARN",
             f"{vpc['native_id']}: {len(active)} active flow log(s)" + (f" to {', '.join(sorted({f['attributes'].get('LogDestinationType', '') for f in active}))}." if active else "; network traffic metadata is not recorded."),
             "Enable VPC Flow Logs (rejected traffic at least) to CloudWatch Logs or S3 with a retention rule.",
             SH + "ec2-controls.html#ec2-6", weight=2, severity="MEDIUM", evidence=vpc["evidence_ids"] + [c["id"] for c in region_calls] + [e for f in active for e in f["evidence_ids"]])

    for eip in [e for e in typed("ec2.elastic-ip") if in_scope(e)]:
        a = eip["attributes"]
        attached = bool(a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId"))
        emit("EIP_UNASSOCIATED", "Cost Optimization", eip, "PASS" if attached else "WARN",
             f"Elastic IP {a.get('PublicIp')} is {'associated' if attached else 'not associated with anything and is billed hourly'}.",
             "Release unassociated Elastic IPs that are not reserved for a documented purpose.", SH + "ec2-controls.html#ec2-12", weight=1, severity="LOW")

    for lb in [l for l in typed("elb.classic-load-balancer") if in_scope(l)]:
        emit("ELB_CLASSIC", "Performance Efficiency", lb, "WARN",
             f"Classic Load Balancer {lb['native_id']} ({lb['attributes'].get('Scheme')}) is a previous-generation load balancer.",
             "Migrate to an Application or Network Load Balancer (the migration wizard keeps listeners and targets); Classic LBs lack HTTP/2, WAF integration and newer TLS policies.",
             "https://docs.aws.amazon.com/elasticloadbalancing/latest/userguide/migrate-to-application-load-balancer.html", weight=1, severity="LOW", related_pillars=["Reliability"])

    for asg in [g for g in typed("autoscaling.group") if in_scope(g)]:
        a = asg["attributes"]
        if a.get("LaunchConfigurationName"):
            emit("ASG_LAUNCH_CONFIGURATION", "Operational Excellence", asg, "WARN",
                 f"Auto Scaling group {asg['name']} uses launch configuration {a['LaunchConfigurationName']}, which is deprecated: no new instance types or features.",
                 "Convert to a launch template (console or `aws autoscaling update-auto-scaling-group --launch-template`); keep the same AMI and user data first, then adopt mixed instances.",
                 "https://docs.aws.amazon.com/autoscaling/ec2/userguide/launch-configurations.html", weight=2, severity="MEDIUM")
        elif a.get("LaunchTemplate") or a.get("MixedInstancesPolicy"):
            emit("ASG_LAUNCH_CONFIGURATION", "Operational Excellence", asg, "PASS", f"Auto Scaling group {asg['name']} uses a launch template.",
                 "Keep the launch template versioned and referenced by version, not $Latest, in production.",
                 "https://docs.aws.amazon.com/autoscaling/ec2/userguide/launch-templates.html", weight=2, severity="MEDIUM")

    previous = TABLES["previous_generation_families"]
    for inst in [i for i in typed("ec2.instance") if in_scope(i)]:
        itype = inst["attributes"].get("InstanceType")
        if not itype:
            continue
        old = family(itype) in previous["ec2"]
        emit("EC2_PREVIOUS_GENERATION", "Cost Optimization", inst, "WARN" if old else "PASS",
             f"{inst['native_id']} runs {itype}" + (" (previous-generation family)." if old else " (current generation)."),
             "Move to the current generation of the same family (or a Graviton equivalent) at the next maintenance window; same price or lower for more performance.",
             "https://aws.amazon.com/ec2/previous-generation/", weight=1, severity="LOW", related_pillars=["Performance Efficiency"])
    for db in [d for d in typed("rds.instance") if in_scope(d)]:
        cls = db["attributes"].get("DBInstanceClass")
        if cls and ".".join(cls.split(".")[:2]) in previous["rds"]:
            emit("EC2_PREVIOUS_GENERATION", "Cost Optimization", db, "WARN", f"{db['native_id']} runs {cls} (previous-generation DB instance class).",
                 "Modify to the current generation class (db.t4g/db.m6g/db.r6g or m7/r7) in a maintenance window.", "https://aws.amazon.com/rds/instance-types/", weight=1, severity="LOW",
                 related_pillars=["Performance Efficiency"])
    for cache in [c for c in typed("elasticache.cluster") if in_scope(c)]:
        node = cache["attributes"].get("CacheNodeType")
        if node and ".".join(node.split(".")[:2]) in previous["elasticache"]:
            emit("EC2_PREVIOUS_GENERATION", "Cost Optimization", cache, "WARN", f"{cache['native_id']} runs {node} (previous-generation cache node type).",
                 "Modify to a current generation node type (cache.t4g/cache.m7g/cache.r7g).", "https://aws.amazon.com/elasticache/pricing/", weight=1, severity="LOW",
                 related_pillars=["Performance Efficiency"])

    licenses = TABLES["commercial_license_usage_operations"]
    for inst in [i for i in typed("ec2.instance") if in_scope(i)]:
        a = inst["attributes"]
        op = str(a.get("UsageOperation") or "")
        product = licenses.get(op) or ("Windows Server" if str(a.get("Platform", "")).lower() == "windows" else None)
        if product:
            emit("COMMERCIAL_LICENSE_DEPENDENCY", "Cost Optimization", inst, "WARN",
                 f"{inst['native_id']} runs {product} (license included, usage operation {op or 'n/a'}).",
                 "Price the open-source alternative (Linux, PostgreSQL/Babelfish for SQL Server) or BYOL on Dedicated Hosts; this is usually the largest single saving in a modernization wave.",
                 "https://aws.amazon.com/windows/resources/licensing/", weight=1, severity="LOW")
    for db in [d for d in typed("rds.instance") if in_scope(d)]:
        engine = str(db["attributes"].get("Engine") or "")
        if (engine.startswith("oracle") or engine.startswith("sqlserver")) and db["attributes"].get("LicenseModel") == "license-included":
            emit("COMMERCIAL_LICENSE_DEPENDENCY", "Cost Optimization", db, "WARN",
                 f"{db['native_id']} runs {engine} {db['attributes'].get('EngineVersion')} with a license-included model.",
                 "Assess a move to Aurora PostgreSQL or RDS PostgreSQL/MySQL with the Schema Conversion Tool and DMS (Babelfish for SQL Server T-SQL); license cost often exceeds the migration cost within a year.",
                 "https://aws.amazon.com/dms/schema-conversion-tool/", weight=1, severity="LOW")

    runtimes = TABLES["lambda_runtime_deprecation"]
    for fn in [f for f in typed("lambda.function") if in_scope(f)]:
        a = fn["attributes"]
        if a.get("PackageType") == "Image":
            emit("LAMBDA_RUNTIME_DEPRECATED", "Security", fn, "NOT_APPLICABLE", f"{fn['name']} is a container image function; the runtime lives in the image.",
                 "Rebuild the image on a supported base image regularly.", "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html", weight=2, severity="MEDIUM")
            continue
        runtime = str(a.get("Runtime") or "")
        key = runtime if runtime in runtimes else ("provided" if runtime == "provided" else None)
        when = parse_date(runtimes.get(key)) if key else None
        if when is None:
            emit("LAMBDA_RUNTIME_DEPRECATED", "Security", fn, "PASS", f"{fn['name']} runtime {runtime or 'unknown'} is not on the deprecation table dated {table_date}.",
                 "Track the Lambda runtime deprecation schedule and upgrade before the deprecation date.", "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html", weight=2, severity="MEDIUM")
            continue
        days = (when - now).days
        status = "FAIL" if days < 0 else ("WARN" if days <= 180 else "PASS")
        emit("LAMBDA_RUNTIME_DEPRECATED", "Security", fn, status,
             f"{fn['name']} runtime {runtime}: deprecation {when.isoformat()} ({'passed ' + str(-days) + ' days ago; no security patches, creation and update blocks follow' if days < 0 else 'in ' + str(days) + ' days'}; table dated {table_date}).",
             "Upgrade to the current runtime of the same language, run the test suite, and set a calendar reminder for the next deprecation.",
             "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html", weight=2, severity="MEDIUM" if days >= 0 else "HIGH", related_pillars=["Operational Excellence"])

    managed = {m["native_id"]: m for m in typed("ssm.managed-instance")}
    ssm_calls = [c for c in evidence.calls.values() if c.get("service") == "ssm" and c.get("operation") == "describe-instance-information" and c.get("status") == "ok"]
    for inst in [i for i in typed("ec2.instance") if in_scope(i) and i["attributes"].get("State", {}).get("Name", "running") == "running"]:
        m = managed.get(inst["native_id"])
        if not m:
            if ssm_calls:
                emit("EC2_OS_END_OF_LIFE", "Security", inst, "UNKNOWN", f"{inst['native_id']} is not an SSM-managed instance; its operating system and version are not visible.",
                     f"Which operating system and version does {inst['native_id']} run, and is it still receiving security updates?",
                     "https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html",
                     question=f"Which operating system and version does {inst['native_id']} run, and is it still receiving security updates?")
            continue
        name, version = str(m["attributes"].get("PlatformName") or ""), str(m["attributes"].get("PlatformVersion") or "")
        match = None
        for entry in TABLES["os_end_of_life"]:
            if entry["match"].lower() in name.lower() and (not entry.get("version_prefix") or version.startswith(entry["version_prefix"])):
                if match is None or len(entry.get("version_prefix", "")) > len(match.get("version_prefix", "")):
                    match = entry
        ev = inst["evidence_ids"] + m["evidence_ids"]
        if not match:
            emit("EC2_OS_END_OF_LIFE", "Security", inst, "PASS", f"{inst['native_id']} runs {name} {version}, not on the end-of-life table dated {table_date}.",
                 "Track the vendor's lifecycle page for this OS.", "https://endoflife.date/", weight=3, severity="HIGH", evidence=ev)
            continue
        days = days_until(match["eol"], now)
        status = "FAIL" if days < 0 else ("WARN" if days <= 365 else "PASS")
        emit("EC2_OS_END_OF_LIFE", "Security", inst, status,
             f"{inst['native_id']} runs {match['label']} ({name} {version}); vendor end of life {match['eol']} ({'passed' if days < 0 else 'in ' + str(days) + ' days'}; table dated {table_date}).",
             "Plan the OS upgrade or replatforming (Amazon Linux 2023, Windows Server 2022, or a container/managed service) before the date; no security patches arrive after it.",
             "https://endoflife.date/", weight=3, severity="HIGH" if days < 0 else "MEDIUM", evidence=ev, related_pillars=["Operational Excellence"])

    # ---- lifecycle dates read from the API (supplementary evidence) ----------------------------------------------
    lifecycle = {}
    for call in evidence.records("rds", "describe-db-major-engine-versions"):
        for version in (evidence.data(call) or {}).get("DBMajorEngineVersions", []):
            lifecycle[(version.get("Engine"), str(version.get("MajorEngineVersion")))] = (call, version.get("SupportedEngineLifecycles") or [])
    for db in [d for d in typed("rds.instance") + typed("rds.cluster") if in_scope(d)]:
        a = db["attributes"]
        engine, version = a.get("Engine"), str(a.get("EngineVersion") or "")
        if not engine or not version:
            continue
        hit = next(((c, l) for (e, major), (c, l) in lifecycle.items() if e == engine and version.startswith(major)), None)
        if not hit:
            if lifecycle or evidence.records("rds", "describe-db-major-engine-versions"):
                emit("RDS_ENGINE_VERSION_SUPPORT", "Reliability", db, "UNKNOWN", f"{db['native_id']}: {engine} {version}; no lifecycle record matched this major version.",
                     f"Confirm the standard-support end date of {engine} {version} on the RDS release calendar.",
                     "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/extended-support.html")
            continue
        call, cycles = hit
        standard = next((c for c in cycles if "standard" in str(c.get("LifecycleSupportName", "")).lower()), None)
        extended = next((c for c in cycles if "extended" in str(c.get("LifecycleSupportName", "")).lower()), None)
        std_days = days_until((standard or {}).get("LifecycleSupportEndDate"), now)
        ext_days = days_until((extended or {}).get("LifecycleSupportEndDate"), now)
        ev = db["evidence_ids"] + [call["id"]]
        if std_days is None:
            emit("RDS_ENGINE_VERSION_SUPPORT", "Reliability", db, "UNKNOWN", f"{db['native_id']}: {engine} {version}; the lifecycle record has no standard-support end date.",
                 f"Confirm the support status of {engine} {version} on the RDS release calendar.", "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/extended-support.html", evidence=ev)
        elif std_days < 0:
            past_ext = ext_days is not None and ext_days < 0
            emit("RDS_ENGINE_VERSION_SUPPORT", "Reliability", db, "FAIL",
                 f"{db['native_id']}: {engine} {version} left standard support on {standard['LifecycleSupportEndDate'][:10]}" +
                 (f" and extended support on {extended['LifecycleSupportEndDate'][:10]}." if past_ext else f"; it is in paid Extended Support{' until ' + str(extended['LifecycleSupportEndDate'])[:10] if extended else ''} (EngineLifecycleSupport={a.get('EngineLifecycleSupport', 'n/a')})."),
                 "Upgrade to a major version in standard support (test with a snapshot restore or a blue/green deployment); extended support adds a per-vCPU-hour charge.",
                 "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/extended-support.html", weight=3, severity="HIGH", evidence=ev, related_pillars=["Cost Optimization"])
        else:
            emit("RDS_ENGINE_VERSION_SUPPORT", "Reliability", db, "WARN" if std_days <= 180 else "PASS",
                 f"{db['native_id']}: {engine} {version} standard support ends {standard['LifecycleSupportEndDate'][:10]} (in {std_days} days).",
                 "Schedule the major version upgrade before standard support ends.", "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/extended-support.html",
                 weight=3, severity="MEDIUM", evidence=ev)

    versions = {}
    for call in evidence.records("eks", "describe-cluster-versions"):
        for v in (evidence.data(call) or {}).get("clusterVersions", []):
            versions[str(v.get("clusterVersion"))] = (call, v)
    for cluster in [c for c in typed("eks.cluster") if in_scope(c)]:
        version = str(cluster["attributes"].get("version") or "")
        if not version:
            continue
        if version not in versions:
            if versions:
                emit("EKS_VERSION_SUPPORT", "Reliability", cluster, "UNKNOWN", f"{cluster['name']}: Kubernetes {version} is not in the version list returned by EKS.",
                     f"Confirm the support status of Kubernetes {version} on the EKS release calendar.", "https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html")
            continue
        call, v = versions[version]
        status, std_days = str(v.get("status") or ""), days_until(v.get("endOfStandardSupportDate"), now)
        ev = cluster["evidence_ids"] + [call["id"]]
        if status.upper() in ("EXTENDED_SUPPORT", "UNSUPPORTED") or (std_days is not None and std_days < 0):
            emit("EKS_VERSION_SUPPORT", "Reliability", cluster, "FAIL",
                 f"{cluster['name']}: Kubernetes {version} is in {status.lower().replace('_', ' ') or 'extended support'}; standard support ended {str(v.get('endOfStandardSupportDate'))[:10]}" +
                 (f", extended support ends {str(v.get('endOfExtendedSupportDate'))[:10]}." if v.get("endOfExtendedSupportDate") else "."),
                 "Upgrade the control plane and node groups one minor version at a time to a version in standard support; extended support is billed per cluster-hour at a higher rate.",
                 "https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html", weight=3, severity="HIGH", evidence=ev, related_pillars=["Cost Optimization"])
        elif std_days is None:
            emit("EKS_VERSION_SUPPORT", "Reliability", cluster, "UNKNOWN", f"{cluster['name']}: Kubernetes {version} status {status or 'unknown'} without an end date.",
                 f"Confirm the support status of Kubernetes {version}.", "https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html", evidence=ev)
        else:
            emit("EKS_VERSION_SUPPORT", "Reliability", cluster, "WARN" if std_days <= 180 else "PASS",
                 f"{cluster['name']}: Kubernetes {version} in standard support until {str(v.get('endOfStandardSupportDate'))[:10]} (in {std_days} days).",
                 "Plan the next minor upgrade before standard support ends; EKS upgrades are sequential, and after that date the cluster is billed at the extended-support hourly rate "
                 "(see the EKS pricing page) until it is upgraded.", "https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html",
                 weight=3, severity="MEDIUM", evidence=ev)

    for call in evidence.records("cloudformation", "describe-stacks"):
        name = evidence.arg(call, "--stack-name")
        if not name:
            continue
        stacks = (evidence.data(call) or {}).get("Stacks") or []
        drift = ((stacks[0] if stacks else {}).get("DriftInformation") or {}).get("StackDriftStatus")
        target = f"stack {name}"
        if drift == "DRIFTED":
            emit("STACK_DRIFT", "Operational Excellence", None, "WARN", f"Stack {name} reports StackDriftStatus=DRIFTED: deployed resources differ from the template.",
                 "Run drift detection, review the drifted properties, and either update the template or redeploy so the template is the truth again.",
                 "https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-stack-drift.html", weight=2, severity="MEDIUM", evidence=[call["id"]], target=target)
        elif drift == "IN_SYNC":
            emit("STACK_DRIFT", "Operational Excellence", None, "PASS", f"Stack {name} is IN_SYNC with its template (last drift check {str(((stacks[0] or {}).get('DriftInformation') or {}).get('LastCheckTimestamp', ''))[:10]}).",
                 "Keep drift detection in the release checklist.", "https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-stack-drift.html", weight=2, severity="MEDIUM", evidence=[call["id"]], target=target)
        elif drift:
            question = f"Drift of stack {name} has never been checked; may we run drift detection, or can the owner run it before the next change?"
            emit("STACK_DRIFT", "Operational Excellence", None, "UNKNOWN", f"Stack {name}: StackDriftStatus={drift}; this read-only assessment does not start drift detection.",
                 question, "https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-stack-drift.html", evidence=[call["id"]], target=target, question=question)

    # ---- edge: CloudFront and certificates ---------------------------------------------------------------------
    for dist in [d for d in typed("cloudfront.distribution") if in_scope(d)]:
        a = dist["attributes"]
        config = (obs(dist, "configuration") or {}).get("DistributionConfig") or {}
        web_acl = a.get("WebACLId") or config.get("WebACLId")
        emit("CLOUDFRONT_WAF", "Security", dist, "PASS" if web_acl else "WARN",
             f"Distribution {a.get('Id')} ({a.get('DomainName')}) {'has' if web_acl else 'has no'} Web ACL" + (f" {str(web_acl).rsplit('/', 2)[-2] if '/' in str(web_acl) else web_acl}." if web_acl else "."),
             "Associate an AWS WAF Web ACL (CLOUDFRONT scope, managed rule groups) and start in count mode.", SH + "cloudfront-controls.html#cloudfront-6", weight=2, severity="MEDIUM")
        cert = a.get("ViewerCertificate") or config.get("ViewerCertificate") or {}
        if cert.get("CloudFrontDefaultCertificate"):
            emit("CLOUDFRONT_TLS_POLICY", "Security", dist, "NOT_APPLICABLE", f"Distribution {a.get('Id')} uses the default *.cloudfront.net certificate; the minimum protocol is fixed by CloudFront.",
                 "Use a custom certificate with TLSv1.2_2021 when a custom domain is added.", SH + "cloudfront-controls.html#cloudfront-10", weight=1, severity="LOW")
        else:
            minimum = str(cert.get("MinimumProtocolVersion") or "")
            weak = minimum in {"SSLv3", "TLSv1", "TLSv1_2016", "TLSv1.1_2016"}
            emit("CLOUDFRONT_TLS_POLICY", "Security", dist, "WARN" if weak else ("PASS" if minimum else "UNKNOWN"),
                 f"Distribution {a.get('Id')} minimum viewer protocol {minimum or 'unknown'}.",
                 "Set the security policy to TLSv1.2_2021 after confirming no client needs TLS 1.0/1.1.", SH + "cloudfront-controls.html#cloudfront-10", weight=1, severity="LOW")
        logging = config.get("Logging") or {}
        if config:
            emit("CLOUDFRONT_LOGGING", "Operational Excellence", dist, "PASS" if logging.get("Enabled") else "WARN",
                 f"Distribution {a.get('Id')} standard logging {'enabled' if logging.get('Enabled') else 'disabled'}.",
                 "Enable standard logging (S3 or CloudWatch Logs) to keep request-level evidence for incidents.", SH + "cloudfront-controls.html#cloudfront-5", weight=1, severity="LOW")

    for cert in [c for c in typed("acm.certificate") if in_scope(c)]:
        a = cert["attributes"]
        in_use = a.get("InUse")
        if in_use is False:
            emit("ACM_CERTIFICATE_EXPIRY", "Reliability", cert, "NOT_APPLICABLE", f"Certificate {a.get('DomainName')} is not attached to any resource.",
                 "Delete unused certificates to reduce noise.", "https://docs.aws.amazon.com/acm/latest/userguide/managed-renewal.html", weight=3, severity="HIGH")
            continue
        days = days_until(a.get("NotAfter"), now)
        status_text = str(a.get("Status") or "")
        if status_text == "EXPIRED" or (days is not None and days < 0):
            status = "FAIL"
        elif days is not None and days <= 30:
            status = "FAIL" if a.get("Type") != "AMAZON_ISSUED" or a.get("RenewalEligibility") == "INELIGIBLE" else "WARN"
        elif days is not None and days <= 90 and a.get("Type") != "AMAZON_ISSUED":
            status = "WARN"
        elif days is None and not status_text:
            status = "UNKNOWN"
        else:
            status = "PASS"
        emit("ACM_CERTIFICATE_EXPIRY", "Reliability", cert, status,
             f"Certificate {a.get('DomainName')} ({a.get('Type', 'type unknown')}, status {status_text or 'unknown'}) expires {str(a.get('NotAfter'))[:10]}" +
             (f" (in {days} days)" if days is not None and days >= 0 else (" (expired)" if days is not None else "")) +
             (f"; renewal eligibility {a.get('RenewalEligibility')}." if a.get("RenewalEligibility") else "."),
             "For imported certificates, replace before expiry; for ACM-issued ones, make sure DNS validation records still exist so managed renewal succeeds.",
             "https://docs.aws.amazon.com/acm/latest/userguide/managed-renewal.html", weight=3, severity="HIGH" if status == "FAIL" else "MEDIUM")
