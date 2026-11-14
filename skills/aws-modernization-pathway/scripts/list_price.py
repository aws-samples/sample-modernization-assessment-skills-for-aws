#!/usr/bin/env python3
"""Published On-Demand list prices from the AWS Price List API, for the cost direction of key issues.

  python3 list_price.py --profile P --output RUN_DIR/05-modernization/list-prices.json \
      rds --engine "Aurora PostgreSQL" --instance-type db.r6g.large --count 1 --label "Aurora reader in us-east-1a"
  python3 list_price.py ... ec2 --instance-type m7i.large --count 2 --label "EKS nodes after right-sizing"
  python3 list_price.py ... elasticache --engine Redis --node-type cache.r6g.large --count 1 --label "Redis replica"
  python3 list_price.py ... eks --support extended --count 1 --label "Extended support surcharge per cluster"

Each call appends one record (p001, p002 ..., or db-p001 with --id-prefix db-) with the hourly price the API returned, the monthly figure for
`count` x 730 hours and the exact basis (region, engine, deployment, what is excluded). The figure is a list price
for a named configuration, not a forecast of the bill: storage, I/O, data transfer, Savings Plans and discounts are
excluded and said so. The call is `pricing get-products` (read-only, covered by ReadOnlyAccess); it fails rather than
guess when the filters match more than one price.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess  # nosec B404 - list-form `aws pricing get-products` only
import sys

HOURS_PER_MONTH = 730
REGION_NAME = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def checked_arguments(arguments):
    """List form only, program name literal at the call site, no control characters in any argument."""
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("command arguments must be a non-empty list")
    for item in arguments:
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("command arguments must be strings without control characters")
    return list(arguments)


def environment(profile):
    env = os.environ.copy()
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off", AWS_EC2_METADATA_DISABLED="true")
    for key in list(env):
        if key.startswith("AWS_ENDPOINT_URL"):
            env.pop(key)
    if profile:
        env.pop("AWS_PROFILE", None)
    return env


def get_products(args, service_code, filters):
    """The Price List API answers in us-east-1 (and a few other endpoints) for every region's prices."""
    argv = ["pricing", "get-products", "--service-code", service_code, "--region", "us-east-1", "--output", "json", "--no-cli-pager",
            "--max-results", "100", "--filters", *[f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in filters.items()]]
    if args.profile:
        argv += ["--profile", args.profile]
    if not shutil.which("aws"):
        raise ValueError("AWS CLI v2 missing")
    response = subprocess.run(["aws", *checked_arguments(argv)], capture_output=True, text=True, env=environment(args.profile), timeout=60)  # nosec B603 B607
    if response.returncode:
        code = re.search(r"\(([A-Za-z0-9_.-]+)\)", response.stderr)
        raise ValueError(f"pricing get-products failed: {code.group(1) if code else 'cli_error'}")
    return [json.loads(p) for p in json.loads(response.stdout).get("PriceList", [])]


def hourly_prices(products, exclude=()):
    """[(usd_per_hour, description, sku)] for On-Demand hourly dimensions, minus descriptions containing an excluded word."""
    out = []
    for product in products:
        for term in (product.get("terms") or {}).get("OnDemand", {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                desc = dim.get("description", "")
                unit = str(dim.get("unit", "")).lower()
                if not unit.startswith("hr") and unit != "hours":
                    continue
                if any(word.lower() in desc.lower() for word in exclude):
                    continue
                usd = float((dim.get("pricePerUnit") or {}).get("USD") or 0)
                if usd > 0:
                    out.append((usd, desc, product.get("product", {}).get("sku")))
    return out


def preset(args):
    region = args.price_region
    if args.kind == "rds":
        return ("AmazonRDS", {"regionCode": region, "instanceType": args.instance_type, "databaseEngine": args.engine,
                              "deploymentOption": "Single-AZ", **({"storage": "EBS Only"} if args.engine.startswith("Aurora") else {})},
                ("unused", "reservation", "IO-optimized", "Extended Support"),
                f"{args.engine} {args.instance_type}, On-Demand, {region}; storage, I/O, backups beyond the free allowance and data transfer excluded")
    if args.kind == "ec2":
        return ("AmazonEC2", {"regionCode": region, "instanceType": args.instance_type, "operatingSystem": args.os, "tenancy": "Shared",
                              "preInstalledSw": "NA", "capacitystatus": "Used"},
                ("Reservation", "Dedicated", "BYOL"),
                f"{args.instance_type} {args.os}, On-Demand, shared tenancy, {region}; EBS, data transfer and Savings Plans excluded")
    if args.kind == "elasticache":
        return ("AmazonElastiCache", {"regionCode": region, "instanceType": args.node_type, "cacheEngine": args.engine},
                ("ExtendedSupport", "Reserved"),
                f"{args.engine} {args.node_type} node, On-Demand, {region}; data transfer and backups excluded")
    if args.kind == "eks":
        operation = "ExtendedSupport" if args.support == "extended" else "CreateOperation"
        return ("AmazonEKS", {"regionCode": region, "operation": operation},
                ("Outposts", "Auto Mode", "Hybrid", "Provisioned"),
                ("EKS extended-support surcharge per cluster on top of the standard cluster hour" if args.support == "extended"
                 else "EKS standard cluster hour") + f", {region}; worker nodes excluded")
    raise ValueError(f"unknown kind {args.kind}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile")
    parser.add_argument("--output", type=Path, required=True, help="list-prices.json; records are appended")
    parser.add_argument("--price-region", default="us-east-1", help="region whose prices are wanted (default us-east-1)")
    parser.add_argument("--id-prefix", default="", help="prefix for record ids (db-, ct- ...) so specialist reviewers writing their own files in parallel never collide")
    sub = parser.add_subparsers(dest="kind", required=True)
    r = sub.add_parser("rds"); r.add_argument("--engine", required=True); r.add_argument("--instance-type", required=True)
    e = sub.add_parser("ec2"); e.add_argument("--instance-type", required=True); e.add_argument("--os", default="Linux")
    c = sub.add_parser("elasticache"); c.add_argument("--engine", default="Redis"); c.add_argument("--node-type", required=True)
    k = sub.add_parser("eks"); k.add_argument("--support", choices=("standard", "extended"), required=True)
    for p in (r, e, c, k):
        p.add_argument("--count", type=int, default=1)
        p.add_argument("--label", required=True, help="what the price is for, in the report's words")
    args = parser.parse_args()
    try:
        if not REGION_NAME.match(args.price_region):
            raise ValueError("--price-region must be a region code")
        if not re.fullmatch(r"(?:[a-z]{2}-)?", args.id_prefix):
            raise ValueError("--id-prefix must be two lowercase letters and a hyphen (for example db-)")
        if not 1 <= args.count <= 1000:
            raise ValueError("--count must be between 1 and 1000")
        service_code, filters, exclude, basis = preset(args)
        prices = hourly_prices(get_products(args, service_code, filters), exclude)
        distinct = sorted({round(p[0], 6) for p in prices})
        if len(distinct) != 1:
            raise ValueError(f"filters matched {len(distinct)} distinct hourly prices ({', '.join(str(d) for d in distinct[:5])}); refine the configuration")
        usd, description, sku = next(p for p in prices if round(p[0], 6) == distinct[0])
        doc = json.loads(args.output.read_text(encoding="utf-8")) if args.output.is_file() else {"schema_version": "1.0", "source": "AWS Price List API (pricing get-products)", "prices": []}
        record = {"id": f"{args.id_prefix}p{len(doc['prices']) + 1:03d}", "label": args.label, "service_code": service_code, "filters": filters, "sku": sku,
                  "usd_per_hour": usd, "count": args.count, "hours_per_month": HOURS_PER_MONTH, "monthly_usd": round(usd * args.count * HOURS_PER_MONTH, 2),
                  "price_description": description, "basis": f"{args.count} x {basis}; {HOURS_PER_MONTH} hours a month",
                  "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        doc["prices"].append(record)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"List price stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
