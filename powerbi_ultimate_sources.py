#!/usr/bin/env python3
"""
List the System(s) each Power BI Report in Collibra ultimately gets its data from.

Uses only the Collibra Knowledge Graph (GraphQL) API:
  https://developer.collibra.com/api/guides/knowledge-graph

How it works
  1. Find all assets of type "Power BI Report".
  2. Walk upstream through catalog relations:
       Power BI Report  <- is source for -  Power BI Data Model
       Power BI Data Model  <- is part of -  Power BI Table
       Power BI Table  - contains ->  Power BI Column
       Power BI Column  <- targets -  Column  <- targets -  Column ... (recursively)
     The walk is done level by level, fetching many assets per query.
  3. The left-most assets (nothing further upstream) are the ultimate sources.
     Leaves that are still Power BI assets (e.g. calculated columns) are ignored.
  4. From each source, climb the containment relations to its System:
       Column  - is part of ->  Table  <- contains -  Schema  <- has -  Database  <- groups -  System

Configuration (environment variables)
  COLLIBRA_URL       e.g. https://your-instance.collibra.com
  COLLIBRA_USER      username
  COLLIBRA_PASSWORD  password
"""

import json
import os
import sys
import time

import requests

BASE_URL = os.environ.get("COLLIBRA_URL", "").rstrip("/")
USER = os.environ.get("COLLIBRA_USER")
PASSWORD = os.environ.get("COLLIBRA_PASSWORD")

GRAPHQL_PATH = "/graphql/knowledgeGraph/v1"
REPORT_ASSET_TYPE = "Power BI Report"

# Out-of-the-box relation type IDs, as seen in the catalog.
DATA_MODEL_FEEDS_REPORT = "00000000-0000-0000-0000-120000000013"  # Power BI Data Model "is source for" Power BI Report
TABLE_PART_OF_MODEL = "00000000-0000-0000-0000-000000007046"      # Power BI Table "is part of" Power BI Data Model
TABLE_CONTAINS_COLUMN = "00000000-0000-0000-0000-000000007047"    # Power BI Table "contains" Power BI Column
COLUMN_TARGETS_COLUMN = "00000000-0000-0000-0000-000000007069"    # Column "targets" Column (column-level lineage)
COLUMN_PART_OF_TABLE = "00000000-0000-0000-0000-000000007042"     # Column "is part of" Table
SCHEMA_CONTAINS_TABLE = "00000000-0000-0000-0000-000000007043"    # Schema "contains" Table
DATABASE_HAS_SCHEMA = "00000000-0000-0000-0000-000000007024"      # Database "has" Schema
SYSTEM_GROUPS_DATABASE = "00000000-0000-0000-0000-000000007054"   # System "groups" Database

# Upstream = the source end of these incoming relations, plus the target end of these outgoing ones.
UPSTREAM_INCOMING = [DATA_MODEL_FEEDS_REPORT, TABLE_PART_OF_MODEL, COLUMN_TARGETS_COLUMN]
UPSTREAM_OUTGOING = [TABLE_CONTAINS_COLUMN]
# Container = the target of these outgoing relations, or the source of these incoming ones.
PARENT_OUTGOING = [COLUMN_PART_OF_TABLE]
PARENT_INCOMING = [SCHEMA_CONTAINS_TABLE, DATABASE_HAS_SCHEMA, SYSTEM_GROUPS_DATABASE]

# Query sizing. The Knowledge Graph caps a query at 100,000 nodes (root limit x child limits),
# so BATCH_SIZE x RELATION_LIMIT x (number of relation fields) must stay under that.
BATCH_SIZE = 20
RELATION_LIMIT = 500
REPORT_PAGE_SIZE = 500
MAX_CLIMB = 6

MAX_RETRIES = 8
REQUEST_DELAY = 0.1

session = requests.Session()
session.auth = (USER, PASSWORD)
session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

# Asset ID -> {"name", "type", "upstream": [ids], "parents": [ids]}
nodes = {}


# --- GraphQL ------------------------------------------------------------------

def graphql(query):
    """POST a GraphQL query, retrying on rate limits; return the `data` object."""
    for attempt in range(MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY)
        resp = session.post(f"{BASE_URL}{GRAPHQL_PATH}", json={"query": query})
        if resp.status_code not in (429, 503) or attempt == MAX_RETRIES:
            break
        retry_after = resp.headers.get("Retry-After", "")
        wait = float(retry_after) if retry_after.isdigit() else min(2 ** attempt, 60)
        print(f"  rate limited (HTTP {resp.status_code}), waiting {wait:.0f}s...", file=sys.stderr)
        time.sleep(wait)

    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        sys.exit(f"Non-JSON response from {resp.url} (HTTP {resp.status_code}):\n{resp.text[:300]}")
    if payload.get("errors"):
        sys.exit("GraphQL error:\n" + json.dumps(payload["errors"], indent=2))
    return payload["data"]


def relation_field(alias, field, type_ids, end):
    """e.g. upIn: incomingRelations(where: {type: {id: {in: [...]}}}, limit: 1000) { source { id } }"""
    where = "{type: {id: {in: " + json.dumps(type_ids) + "}}}"
    return f"{alias}: {field}(where: {where}, limit: {RELATION_LIMIT}) {{ {end} {{ id }} }}"


NODE_FIELDS = " ".join([
    "id displayName fullName type { name }",
    relation_field("upIn", "incomingRelations", UPSTREAM_INCOMING, "source"),
    relation_field("upOut", "outgoingRelations", UPSTREAM_OUTGOING, "target"),
    relation_field("parentOut", "outgoingRelations", PARENT_OUTGOING, "target"),
    relation_field("parentIn", "incomingRelations", PARENT_INCOMING, "source"),
])


def fetch_nodes(ids):
    """Load name, type, upstream and container links for any asset IDs not already cached."""
    missing = sorted({i for i in ids if i not in nodes})
    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start:start + BATCH_SIZE]
        query = (f"{{ assets(where: {{id: {{in: {json.dumps(batch)}}}}}, limit: {len(batch)}) "
                 f"{{ {NODE_FIELDS} }} }}")
        for asset in graphql(query)["assets"] or []:
            for alias in ("upIn", "upOut", "parentOut", "parentIn"):
                if len(asset[alias]) >= RELATION_LIMIT:
                    print(f"  warning: {asset['id']} hit the {RELATION_LIMIT}-relation limit "
                          f"on {alias}; results may be incomplete", file=sys.stderr)
            nodes[asset["id"]] = {
                "name": asset.get("displayName") or asset["fullName"],
                "type": asset["type"]["name"],
                "upstream": [r["source"]["id"] for r in asset["upIn"]]
                            + [r["target"]["id"] for r in asset["upOut"]],
                "parents": [r["target"]["id"] for r in asset["parentOut"]]
                           + [r["source"]["id"] for r in asset["parentIn"]],
            }
        for i in batch:  # not returned: deleted, or no permission to view
            nodes.setdefault(i, {"name": "(not accessible)", "type": "", "upstream": [], "parents": []})


def get_reports():
    """All Power BI Report assets, paged by ID (keyset pagination, as the docs recommend)."""
    reports, last_id = [], None
    while True:
        where = "type: {name: {eq: " + json.dumps(REPORT_ASSET_TYPE) + "}}"
        if last_id:
            where += f', id: {{gt: "{last_id}"}}'
        query = (f"{{ assets(where: {{{where}}}, order: {{id: asc}}, limit: {REPORT_PAGE_SIZE}) "
                 f"{{ id displayName fullName }} }}")
        page = graphql(query)["assets"] or []
        reports += page
        if len(page) < REPORT_PAGE_SIZE:
            return reports
        last_id = page[-1]["id"]


# --- Lineage walk -------------------------------------------------------------

def walk_upstream(start_ids):
    """Breadth-first: fetch each level of upstream assets in batches until none are left."""
    frontier, depth = set(start_ids), 0
    while frontier:
        depth += 1
        print(f"  level {depth}: fetching {len(frontier)} assets", file=sys.stderr)
        fetch_nodes(frontier)
        frontier = {up for i in frontier for up in nodes[i]["upstream"] if up not in nodes}


def ultimate_sources(report_id):
    """Left-most assets reachable upstream from a report, excluding Power BI assets."""
    leaves, seen, stack = set(), {report_id}, [report_id]
    while stack:
        current = stack.pop()
        upstream = nodes[current]["upstream"]
        if not upstream and current != report_id:
            leaves.add(current)
        for up in upstream:
            if up not in seen:
                seen.add(up)
                stack.append(up)
    return {leaf for leaf in leaves if not nodes[leaf]["type"].startswith("Power BI")}


def find_systems(source_ids):
    """Climb containment from each source; return {source_id: top container id}."""
    top = {s: s for s in source_ids}
    for _ in range(MAX_CLIMB):
        fetch_nodes(top.values())
        moved = False
        for source, current in top.items():
            if nodes[current]["parents"]:
                top[source] = nodes[current]["parents"][0]
                moved = True
        if not moved:
            break
    fetch_nodes(top.values())
    return top


def system_label(asset_id):
    node = nodes[asset_id]
    if node["type"] == "System":
        return node["name"]
    return f"{node['name']} [{node['type'] or 'unknown'}; no System linked]"


# --- Main ---------------------------------------------------------------------

def main():
    if not (BASE_URL and USER and PASSWORD):
        sys.exit("Set COLLIBRA_URL, COLLIBRA_USER and COLLIBRA_PASSWORD")

    reports = get_reports()
    print(f"Found {len(reports)} Power BI reports. Walking upstream...", file=sys.stderr)
    walk_upstream(r["id"] for r in reports)

    sources_by_report = {r["id"]: ultimate_sources(r["id"]) for r in reports}
    all_sources = set().union(*sources_by_report.values()) if reports else set()
    print(f"Finding systems for {len(all_sources)} source assets...", file=sys.stderr)
    system_of = find_systems(all_sources)

    rows = []
    for report in reports:
        name = report.get("displayName") or report["fullName"]
        systems = {system_of[s] for s in sources_by_report[report["id"]]}
        if not systems:
            rows.append((name, report["id"], "(no upstream source found)", ""))
        for system_id in sorted(systems, key=system_label):
            rows.append((name, report["id"], system_label(system_id), system_id))

    headers = ("Report Name", "Report ID", "System Name", "System ID")
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))


if __name__ == "__main__":
    main()
