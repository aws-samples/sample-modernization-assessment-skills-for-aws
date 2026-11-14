#!/usr/bin/env python3
"""Import Service Screener v2 results (api-full.json) as separately-sourced findings.

Service Screener (https://github.com/aws-samples/service-screener-v2) is run by the account owner
with their own permissions; its execution is not part of this read-only assessment. This helper only
reads an existing `api-full.json`, verifies that it belongs to the assessed account and regions, and
maps each listed failure to the findings contract with `source: service-screener`. It never
impersonates upstream results as local checks, never changes upstream severities and never writes to
AWS.

  python3 import_service_screener.py --as-is RUN_DIR/03-as-is --input api-full.json \\
      --output RUN_DIR/04-review/upstream [--commit <upstream git sha>] [--allow-region-superset]

Outputs under --output: a byte-identical copy of the input (api-full.json), upstream-manifest.json
(sha256, account, regions, counts, commit) and screener-findings.json (mapped findings). Pass
screener-findings.json to review.py --screener so the report gains a Security recommendations
chapter that keeps upstream and local findings side by side.

Schema verified at upstream commit 055c882bbda3dcea831ed81ee41f371030e25b11 (2026-10-26):
data[service]["detail"][region][resource_id][check] = {criticality H|M|L|I, shortDesc, __categoryMain
S|O|T|R|P|C|CP, value}; data[service]["summary"][check] carries ^description, __links, remediation_doc
and __affectedResources; data["__metadata"] carries accountId and regions. Only failures are listed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys

CATEGORY = {"S": "Security", "O": "Operational Excellence", "T": "Operational Excellence", "R": "Reliability",
            "P": "Performance Efficiency", "C": "Cost Optimization", "CP": "Cost Optimization"}
CRITICALITY = {"H": "HIGH", "M": "MEDIUM", "L": "LOW", "I": "INFO"}
SOURCE = "service-screener"
UPSTREAM = "https://github.com/aws-samples/service-screener-v2"
RESERVED = {"__metadata", "__suppressed", "ta", "coh", "findings", "modernize", "stats"}


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def links_from(summary_check):
    urls = []
    for item in summary_check.get("__links", []) or []:
        for url in re.findall(r"href=['\"]([^'\"]+)['\"]", str(item)):
            urls.append(url)
    doc = summary_check.get("remediation_doc")
    if isinstance(doc, str) and doc.startswith("http"):
        urls.append(doc)
    return list(dict.fromkeys(u for u in urls if u.startswith("http")))


def load_inventory(as_is):
    inventory = json.loads((Path(as_is) / "inventory.json").read_text(encoding="utf-8"))
    by_native = {}
    for r in inventory["resources"]:
        by_native.setdefault(r["native_id"], r["id"])
        by_native.setdefault(r["name"], r["id"])
        tail = r["native_id"].rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        by_native.setdefault(tail, r["id"])
    return inventory, by_native


def map_findings(data, inventory, by_native, evidence_id):
    account = inventory["account_id"]
    findings, unmatched = [], 0
    counter = 0
    for service, block in sorted(data.items()):
        if service in RESERVED or not isinstance(block, dict) or not isinstance(block.get("detail"), dict):
            continue
        summary = block.get("summary") if isinstance(block.get("summary"), dict) else {}
        for region, resources in sorted(block["detail"].items()):
            if not isinstance(resources, dict):
                continue
            for resource_id, checks in sorted(resources.items()):
                if not isinstance(checks, dict):
                    continue
                for check, item in sorted(checks.items()):
                    if not isinstance(item, dict) or "criticality" not in item:
                        continue
                    counter += 1
                    pillar = CATEGORY.get(str(item.get("__categoryMain", "")).upper())
                    severity = CRITICALITY.get(str(item.get("criticality", "")).upper())
                    if pillar is None or severity is None:
                        continue
                    key = resource_id.split("::", 1)[-1]
                    local = by_native.get(resource_id) or by_native.get(key) or by_native.get(key.rsplit("/", 1)[-1])
                    if local is None:
                        unmatched += 1
                    sc = summary.get(check, {}) if isinstance(summary.get(check), dict) else {}
                    description = strip_html(sc.get("^description") or "").replace("{$COUNT}", "")
                    findings.append({
                        "id": f"ss{counter:04d}", "rule_id": f"SS::{service}::{check}", "pillar": pillar, "source": SOURCE,
                        "resource_id": local, "upstream_resource": f"{service}::{region}::{resource_id}", "region": region,
                        "status": "FAIL", "severity": severity, "weight": 2, "evidence_ids": [evidence_id],
                        "observation": strip_html(item.get("shortDesc") or check) + (f". {description}" if description else ""),
                        "recommendation": strip_html(sc.get("remediation_risk") or "") or "Follow the remediation in the referenced AWS documentation.",
                        "source_urls": links_from(sc) or [UPSTREAM], "confidence": "configuration",
                        "upstream": {"service": service, "check": check, "criticality": item.get("criticality"), "category": item.get("__categoryMain"),
                                     "value": item.get("value") if isinstance(item.get("value"), (str, int, float, bool)) else None},
                    })
    return findings, unmatched


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-is", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="Service Screener api-full.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", help="upstream Service Screener git commit used for the run, if known")
    parser.add_argument("--allow-region-superset", action="store_true", help="accept Screener runs that cover more regions than the snapshot")
    args = parser.parse_args()
    try:
        raw = args.input.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("api-full.json must be an object")
        inventory, by_native = load_inventory(args.as_is)
        meta = data.get("__metadata") or {}
        account = str(meta.get("accountId") or "")
        if account != inventory["account_id"]:
            raise ValueError(f"account mismatch: screener {account or 'unknown'} vs snapshot {inventory['account_id']}")
        regions = [r for r in (meta.get("regions") or []) if r != "GLOBAL"]
        extra = sorted(set(regions) - set(inventory["regions"]))
        if extra and not args.allow_region_superset:
            raise ValueError(f"screener covers regions outside the snapshot: {', '.join(extra)}")
        digest = hashlib.sha256(raw).hexdigest()
        args.output.mkdir(parents=True, exist_ok=True)
        copy = args.output / "api-full.json"
        if copy.exists() and hashlib.sha256(copy.read_bytes()).hexdigest() != digest:
            raise ValueError("a different api-full.json already exists in the output directory")
        shutil.copyfile(args.input, copy)
        evidence_id = "ss-api-full"
        findings, unmatched = map_findings(data, inventory, by_native, evidence_id)
        manifest = {"schema_version": "1.0", "source": SOURCE, "upstream": UPSTREAM, "commit": args.commit,
                    "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "input_sha256": digest,
                    "account_id": account, "regions": regions, "services": sorted(k for k in data if k not in RESERVED and isinstance(data[k], dict) and "detail" in data[k]),
                    "findings": len(findings), "unmatched_resources": unmatched,
                    "evidence": {evidence_id: {"path": "api-full.json", "sha256": digest}},
                    "note": "Upstream failures only; passing checks are not listed by Service Screener. Severities are upstream criticality, kept separate from local severities."}
        (args.output / "upstream-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output / "screener-findings.json").write_text(json.dumps({"schema_version": "1.0", "source": SOURCE, "account_id": account,
                                                                       "input_sha256": digest, "checks": findings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "imported", "findings": len(findings), "unmatched_resources": unmatched, "regions": regions, "output": str(args.output)}))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
