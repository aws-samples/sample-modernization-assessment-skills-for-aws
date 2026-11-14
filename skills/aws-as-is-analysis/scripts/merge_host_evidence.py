#!/usr/bin/env python3
"""Create a derived AWS snapshot containing verified EC2 host metadata evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def merge(as_is, host_dir, output):
    review = load_module("moda_host_review", HERE.parents[1] / "aws-architecture-review/scripts/review.py")
    host = load_module("moda_host_inspect", HERE / "inspect_host.py")
    inventory, manifest, calls = review.verify_inputs(as_is)
    metadata = json.loads((host_dir / "host-manifest.json").read_text(encoding="utf-8"))
    target = metadata.get("verified_target") or {}
    if metadata.get("status") not in {"observed", "partial"}:
        raise ValueError("host_probe_not_successful")
    if target.get("account_id") != inventory["account_id"] or target.get("region") not in inventory["regions"]:
        raise ValueError("host_snapshot_account_or_region_mismatch")
    matching = [r for r in inventory["resources"] if r["type"] == "ec2.instance"
                and r["native_id"] == target.get("instance_id") and r["region"] == target["region"]]
    if len(matching) != 1:
        raise ValueError("host_not_bound_to_inventory_ec2")
    instance = matching[0]
    if "host_metadata" in instance.get("observations", {}):
        raise ValueError("host_evidence_already_attached_use_fresh_base")
    evidence = metadata.get("evidence") or {}
    raw = (host_dir / evidence.get("raw_path", "")).resolve()
    if not raw.is_relative_to(host_dir.resolve()) or not raw.is_file():
        raise ValueError("host_evidence_path_invalid")
    if hashlib.sha256(raw.read_bytes()).hexdigest() != evidence.get("sha256"):
        raise ValueError("host_evidence_checksum_mismatch")
    data = host.parse_probe(raw.read_text(encoding="utf-8"))
    if data["status"] != metadata["status"]:
        raise ValueError("host_status_mismatch")
    if output.exists() or output.resolve().is_relative_to(as_is.resolve()):
        raise ValueError("fresh_output_outside_input_required")
    number = 1
    while f"h{number:04d}" in calls:
        number += 1
    eid = f"h{number:04d}"
    record = {"id": eid, "service": "host", "operation": "fixed-metadata-probe", "region": target["region"],
              "arguments": [target["instance_id"], metadata["transport"]],
              "status": "ok", "host_status": data["status"], "error_code": None,
              "collected_at": metadata["completed_at"], "raw_path": f"raw/{eid}.json",
              "sha256": evidence["sha256"], "probe_sha256": metadata.get("probe_sha256"),
              "command_id": metadata.get("command_id")}
    manifest["calls"].append(record)
    manifest["coverage"]["ok"] = manifest["coverage"].get("ok", 0) + 1
    manifest["host_inspections_added"] = manifest.get("host_inspections_added", 0) + 1
    manifest["host_collection_note"] = "Host records are evidence units. api_calls_executed retains original AWS collector count; SSH/SSM transport calls are not included."
    manifest["derived_at"] = datetime.now(timezone.utc).isoformat()
    manifest["completed_at"] = max(manifest["completed_at"], metadata["completed_at"])
    # The combined snapshot contains remote execution evidence, not only AWS reads.
    manifest["read_only"] = False
    manifest["application_changes_requested"] = False
    if "host" not in manifest["services"]:
        manifest["services"].append("host")
    inventory["services"] = list(manifest["services"])
    instance.setdefault("observations", {})["host_metadata"] = {
        "status": "ok", "host_status": data["status"], "data": data, "evidence_id": eid}
    instance["evidence_ids"].append(eid)
    for index, app in enumerate(data["applications"]):
        if app.get("status") not in {"observed", "partial"}:
            continue
        rid = f"{inventory['account_id']}:{target['region']}:host.application:{target['instance_id']}:{index}"
        inventory["resources"].append({"id": rid, "type": "host.application",
            "native_id": f"{target['instance_id']}:{index}", "name": app["root"],
            "account_id": inventory["account_id"], "region": target["region"],
            "attributes": app, "observations": {}, "evidence_ids": [eid]})
        inventory["relationships"].append({"source": rid, "target": instance["id"], "relation": "observed_on",
            "evidence_ids": [eid], "certainty": "observed_host_metadata; deployed_tree_not_loaded_code_proof"})
    if data["status"] == "partial":
        manifest["status"] = "partial"
        manifest["truncations"].append({"scope": target["instance_id"], "reason": "host_coverage_incomplete",
                                        "limitations": data["limitations"]})
    inventory["collection_status"] = manifest["status"]
    shutil.copytree(as_is, output)
    output.chmod(0o700)
    shutil.copy2(raw, output / record["raw_path"])
    for name, value in [("inventory.json", inventory), ("collection-manifest.json", manifest)]:
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        path.chmod(0o600)
    report = output / "as-is-report.md"
    with report.open("a", encoding="utf-8") as stream:
        stream.write(f"\n\n## Host evidence supplement\n\nTarget `{target['instance_id']}`, Evidence `{eid}`. Fixed SSH/SSM metadata inspection results are included.\n"
                     "The original AWS snapshot is preserved. A deployed tree revision and code actually loaded by a process require separate verification.\n")
    review.verify_inputs(output)
    return eid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--as-is", type=Path, required=True)
    p.add_argument("--host-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        eid = merge(args.as_is, args.host_dir, args.output)
        print(json.dumps({"status": "merged", "evidence_id": eid, "output": str(args.output)}))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"Host merge stopped: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
