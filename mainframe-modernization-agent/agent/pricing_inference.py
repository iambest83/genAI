"""Pricing argument inference for the keyword router.

When an SA asks "how much does m5.4xlarge cost in us-east-1?" the router can
emit a `get_pricing` call with fully structured Pricing API filters directly,
skipping the discovery tool (`get_pricing_service_codes`) and giving Sonnet
real numbers to cite rather than hallucinating from training data.

The inference is conservative: only emit `get_pricing` when we're confident
about all three (service, instance/product type, region). When any is missing,
the caller falls back to `get_pricing_service_codes` so Sonnet can chain the
follow-up call.
"""
from __future__ import annotations

import re

# Instance-type patterns like "m5.4xlarge", "db.r6i.xlarge", "cache.r7g.large".
INSTANCE_RE = re.compile(
    r"\b((?:db|cache|ml)\.)?"           # optional service prefix
    r"([a-z][0-9][a-z]*)"               # family + gen + suffix (m5, r6i, c7g, t4g, ...)
    r"\.([0-9]*x?[a-z]+)\b",            # size (xlarge, 4xlarge, medium, ...)
    re.IGNORECASE,
)

# AWS region codes like "us-east-1", "eu-west-2", "ap-southeast-3".
REGION_RE = re.compile(
    r"\b(af|ap|ca|cn|eu|me|sa|us|us-gov)-(?:north|south|east|west|central|northeast|southeast|southwest|northwest)-[1-9]\b",
    re.IGNORECASE,
)

# Explicit service mentions that override the instance-prefix heuristic.
SERVICE_KEYWORDS = {
    "AmazonEC2": ["ec2", "amazon ec2"],
    "AmazonRDS": ["rds", "amazon rds"],
    "AmazonElastiCache": ["elasticache", "elasti cache"],
    "AmazonS3": ["amazon s3", " s3 "],
    "AmazonDynamoDB": ["dynamodb", "dynamo db"],
    "AWSLambda": ["lambda pricing", "aws lambda"],
    "AWSMainframeModernization": ["mainframe modernization", "m2 service", "aws m2"],
    "AWSDataMigrationSvc": ["dms", "data migration service", "database migration"],
}

# Map instance-type prefix → service code.
PREFIX_TO_SERVICE = {
    "db.": "AmazonRDS",
    "cache.": "AmazonElastiCache",
    "ml.": "AmazonSageMaker",
}

# Region long-names for the Pricing API `location` filter. AmazonEC2 uses
# `location` (long name) not `regionCode` (short) for its primary index.
REGION_LONG_NAMES = {
    "us-east-1":      "US East (N. Virginia)",
    "us-east-2":      "US East (Ohio)",
    "us-west-1":      "US West (N. California)",
    "us-west-2":      "US West (Oregon)",
    "eu-west-1":      "Europe (Ireland)",
    "eu-west-2":      "Europe (London)",
    "eu-west-3":      "Europe (Paris)",
    "eu-central-1":   "Europe (Frankfurt)",
    "eu-north-1":     "Europe (Stockholm)",
    "eu-south-1":     "Europe (Milan)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-south-1":     "Asia Pacific (Mumbai)",
    "ap-east-1":      "Asia Pacific (Hong Kong)",
    "ca-central-1":   "Canada (Central)",
    "sa-east-1":      "South America (São Paulo)",
    "af-south-1":     "Africa (Cape Town)",
    "me-south-1":     "Middle East (Bahrain)",
}


def infer_pricing_args(query: str) -> dict | None:
    """Try to build a `get_pricing` args dict from the query.

    Returns a dict with `service_code`, `region`, `filters`, and
    `output_options` if we can pattern-match all three. Returns None
    when the query is too vague.
    """
    q = query.lower()

    region_match = REGION_RE.search(q)
    if not region_match:
        return None
    region = region_match.group(0).lower()

    inst_match = INSTANCE_RE.search(q)
    if not inst_match:
        return None
    prefix = (inst_match.group(1) or "").lower()
    family_gen = inst_match.group(2).lower()
    size = inst_match.group(3).lower()
    instance_type = f"{prefix}{family_gen}.{size}"

    service_code = None
    for svc, kws in SERVICE_KEYWORDS.items():
        if any(k in q for k in kws):
            service_code = svc
            break
    if not service_code:
        service_code = PREFIX_TO_SERVICE.get(prefix, "AmazonEC2")

    filters = [{"Field": "instanceType", "Value": instance_type, "Type": "TERM_MATCH"}]

    if service_code == "AmazonEC2":
        long_name = REGION_LONG_NAMES.get(region)
        if long_name:
            filters.append({"Field": "location", "Value": long_name, "Type": "TERM_MATCH"})
        if "windows" in q:
            filters.append({"Field": "operatingSystem", "Value": "Windows", "Type": "TERM_MATCH"})
        else:
            filters.append({"Field": "operatingSystem", "Value": "Linux", "Type": "TERM_MATCH"})
        filters.append({"Field": "tenancy", "Value": "Shared", "Type": "TERM_MATCH"})
        filters.append({"Field": "preInstalledSw", "Value": "NA", "Type": "TERM_MATCH"})
        filters.append({"Field": "capacitystatus", "Value": "Used", "Type": "TERM_MATCH"})

    return {
        "service_code": service_code,
        "region": region,
        "filters": filters,
        "output_options": {"pricing_terms": ["OnDemand"]},
    }
