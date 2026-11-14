# Service Screener v2 integration

Source: https://github.com/aws-samples/service-screener-v2
Reviewed commit: 055c882bbda3dcea831ed81ee41f371030e25b11, 2026-10-16.

## Default read-only assessment

This project adapts the metadata-to-resource-rules-to-framework-findings/reporting approach, with its own starter rules and scores. It does not impersonate upstream rule IDs/results or vendor the entire implementation. Reference files included Ec2SecGroup.py, service reporter definitions and OutputGenerator.py.

## Import existing results

`scripts/import_service_screener.py` reads `api-full.json` (schema verified at the reviewed commit:
`data[service].detail[region][resource_id][check]` with `criticality` H/M/L/I, `shortDesc` and
`__categoryMain` S/O/T/R/P/C/CP; `data[service].summary[check]` with `^description`, `__links`,
`remediation_doc`; `data.__metadata.accountId/regions`). It rejects another account or extra
regions, copies the input byte-for-byte with its SHA-256 into `04-review/upstream/`, maps every
listed failure to the findings contract with `source: service-screener`, `rule_id SS::<service>::<check>`
and the upstream criticality as severity, links resources to the local inventory where identifiers
match, and writes `screener-findings.json`. Service Screener lists failures only, so absence of a
check is not a pass. Preserve the HTML report and run parameters the user supplies as well. Ambiguous
status remains UNKNOWN. Source and local severity stay separate; upstream findings never enter local
scores or grades. The assessment report lists them as ordinary security items without naming the tool;
the tool name and input hash are recorded in `assessment-summary.json` (`imported_findings`).

## If upstream execution is requested

Recheck the current README, argument parser, CfnTrail and main implementation for options and side effects. The reviewed version creates/deletes a CloudFormation marker; WA integration may write workloads/milestones. Read-only authorization does not authorize those writes.

If execution and its necessary writes are already authorized, proceed within scope. Otherwise continue the local read-only workflow, asking about specific writes only if upstream execution is genuinely required. Use a virtual environment, inspect current dependencies and verify CLI help. Do not default to all regions.

```bash
python main.py --regions ap-northeast-2 --services ec2,rds,s3 --profile assessment
```

At the reviewed commit the suppression option is --suppress_file; inspect the actual parser when documentation differs. Verify option types rather than guessing. Review output locally, without publishing account results or treating upstream example costs as actual workload cost.
