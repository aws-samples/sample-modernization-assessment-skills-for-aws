#!/usr/bin/env python3
"""Import sanitized workload evidence into a fresh snapshot; no AWS calls."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
STATES = {"ok", "access_denied", "expired_token", "not_found", "throttled", "error", "skipped"}
KINDS = {"configuration", "template_declared", "source_declared", "documented", "measured"}
# Operations whose documented "nothing is attached/enabled" answer is an empty body with exit 0.
# A blank stdout from these is a judged absence, not missing evidence; the collector records a
# `{}` payload and the review treats the absence as a finding. Everything else keeps the rule
# "never manufacture a successful empty result from blank stdout".
EMPTY_MEANS_ABSENT = {
    ("wafv2", "get-web-acl-for-resource"): "no_web_acl_associated",
    ("waf-regional", "get-web-acl-for-resource"): "no_web_acl_associated",
    ("bedrock", "get-model-invocation-logging-configuration"): "no_invocation_logging",
}


def load_review():
    spec = importlib.util.spec_from_file_location(
        "supplement_review", HERE.parents[1] / "aws-architecture-review/scripts/review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamp must include timezone")
    return parsed


def validate_record(record, root, inventory, known_ids, review=None):
    """Validate declared provenance and payload; this cannot authenticate collection."""
    if not isinstance(record, dict):
        raise ValueError("evidence record must be an object")
    eid = record.get("id")
    if not isinstance(eid, str) or not re.fullmatch(r"s[0-9]{4,}", eid) or eid in known_ids:
        raise ValueError("invalid or duplicate supplementary evidence ID")
    if record.get("status") not in STATES or record.get("evidence_kind") not in KINDS:
        raise ValueError("unsupported evidence status or kind")
    if record.get("region") not in inventory["regions"]:
        raise ValueError("supplement region outside snapshot")
    for key in ("service", "operation", "collected_at"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"evidence requires {key}")
    timestamp(record["collected_at"])
    for key in ("arguments", "resource_ids"):
        if not isinstance(record.get(key), list) or any(not isinstance(v, str) for v in record[key]):
            raise ValueError(f"{key} must be a string array")
    if set(record["resource_ids"]) - {r["id"] for r in inventory["resources"]}:
        raise ValueError("supplement references unknown resource")
    if not isinstance(record.get("response_complete"), bool):
        raise ValueError("response_complete must be boolean")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("origin"), str) or not provenance["origin"].strip():
        raise ValueError("evidence origin required")
    if not isinstance(provenance.get("redactions"), list) or any(
            not isinstance(v, str) for v in provenance["redactions"]):
        raise ValueError("redactions must name removed fields without values")
    if record["evidence_kind"] == "source_declared" and not (
            isinstance(provenance.get("revision"), str) and provenance["revision"].strip()):
        raise ValueError("source evidence requires revision")
    method = record.get("collection_method")
    if method not in {"aws_cli", "local_file"}:
        raise ValueError("unsupported collection method")
    if method == "aws_cli" and record["status"] != "skipped":
        code = record.get("return_code")
        validation_failure = (record["status"] == "error"
                              and isinstance(record.get("validation_error"), str)
                              and bool(record["validation_error"].strip()))
        if type(code) is not int or (record["status"] == "ok" and code != 0) or (
                code == 0 and record["status"] != "ok" and not validation_failure):
            raise ValueError("CLI exit code conflicts with evidence status")
    if record["status"] != "ok" and not record.get("error_code"):
        raise ValueError("failed/skipped evidence requires error or reason code")
    raw = None
    if record.get("raw_path"):
        path = (root / record["raw_path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("supplement path escapes directory or is missing")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise ValueError("supplement checksum mismatch")
    if record["status"] == "ok":
        if raw is None:
            raise ValueError("successful evidence requires payload")
        meaning = EMPTY_MEANS_ABSENT.get((record.get("service"), record.get("operation")))
        if meaning and raw.strip() in {b"", b"{}"}:
            # Documented empty answer: keep an explicit, parseable payload and say what it means.
            record["empty_response_meaning"] = meaning
            if raw.strip() == b"":
                raise ValueError("record the documented empty answer as '{}' so the payload stays parseable")
            return raw
        if record.get("raw_format") not in {"json", "text"}:
            raise ValueError("successful supplement requires raw_format")
        (review or load_review()).validate_payload(record, raw)
        if record.get("raw_format") == "text" and record["evidence_kind"] not in {
                "source_declared", "template_declared", "documented"}:
            raise ValueError("metadata and measurements require structured JSON")
    return raw


def merge(as_is, supplement_dir, output):
    review = load_review()
    inventory, manifest, calls = review.verify_inputs(as_is)
    metadata = json.loads((supplement_dir / "supplementary-manifest.json").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != "1.0" or metadata.get("account_id") != inventory["account_id"]:
        raise ValueError("supplement schema/account mismatch")
    regions = metadata.get("regions")
    if not isinstance(regions, list) or not regions or any(r not in inventory["regions"] for r in regions):
        raise ValueError("supplement regions outside snapshot")
    records = metadata.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("supplement records required")
    if output.exists() or any(output.resolve().is_relative_to(p.resolve()) for p in (as_is, supplement_dir)):
        raise ValueError("fresh output outside inputs required")
    known_ids = set(calls)
    payloads = {}
    additions = []
    for original in records:
        record = deepcopy(original)
        if record.get("region") not in regions:
            raise ValueError("record region missing from supplement scope")
        raw = validate_record(record, supplement_dir, inventory, known_ids, review)
        known_ids.add(record["id"])
        if raw is not None:
            extension = ".json" if record.get("raw_format") == "json" else ".txt"
            record["raw_path"] = f"raw/supplementary/{record['id']}{extension}"
            payloads[record["raw_path"]] = raw
        else:
            record["raw_path"] = None
            record["sha256"] = None
        additions.append(record)
    manifest["calls"].extend(additions)
    manifest["coverage"] = dict(Counter(c["status"] for c in manifest["calls"]))
    manifest["supplementary_evidence_added"] = manifest.get("supplementary_evidence_added", 0) + len(additions)
    manifest["supplementary_collection_note"] = (
        "Supplements are evidence records; api_calls_executed retains the original collector count.")
    manifest["derived_at"] = datetime.now(timezone.utc).isoformat()
    manifest["completed_at"] = max(
        [manifest["completed_at"]] + [r["collected_at"] for r in additions], key=timestamp)
    for record in additions:
        if record["status"] != "ok" or not record["response_complete"]:
            manifest["status"] = "partial"
            manifest["truncations"].append({
                "scope": record["id"], "reason": record.get("error_code") or "supplement_incomplete"})
    # Supplementary services are recorded on calls; the base service inventory
    # scope is unchanged because importing a template does not inventory a service.
    inventory["collection_status"] = manifest["status"]
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".supplement-", dir=output.parent))
    try:
        shutil.copytree(as_is, staging, dirs_exist_ok=True)
        staging.chmod(0o700)
        for relative, raw in payloads.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o600)
        for name, value in (("inventory.json", inventory), ("collection-manifest.json", manifest)):
            path = staging / name
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.chmod(0o600)
        scope_file = supplement_dir / "scope-resources.json"
        if scope_file.is_file():
            # Workload scope resolved by supplement.py travels with the derived snapshot.
            scope = json.loads(scope_file.read_text(encoding="utf-8"))
            known = {r["id"] for r in inventory["resources"]}
            if scope.get("account_id") != inventory["account_id"] or any(x["id"] not in known for x in scope.get("resources", [])):
                raise ValueError("scope-resources.json does not match the snapshot")
            (staging / "scope-resources.json").write_text(json.dumps(scope, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        with (staging / "as-is-report.md").open("a", encoding="utf-8") as stream:
            stream.write("\n\n## Supplementary evidence\n\n"
                         f"Imported {len(additions)} evidence records into this derived snapshot. "
                         "Update the workload narrative from the combined evidence before review. "
                         "Source/template declarations do not prove the loaded runtime revision.\n")
        review.verify_inputs(staging)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return [r["id"] for r in additions]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-is", type=Path, required=True)
    parser.add_argument("--supplement-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        ids = merge(args.as_is, args.supplement_dir, args.output)
        print(json.dumps({"status": "merged", "evidence_ids": ids, "output": str(args.output)}))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # Do not echo raw payloads or arbitrary JSON parse excerpts.
        print(f"Supplement merge stopped: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
