#!/usr/bin/env python3
"""
Notion Project Tracker for Automation Pipeline
Creates a database and pre-populates tasks for the Trustate/GHL automation project.
"""

import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Phase options in order
PHASES = ["Planning", "Research", "Dev", "Testing", "Deploy", "Monitor"]

# Status options
STATUSES = ["Not Started", "In Progress", "Blocked", "Done"]

# Priority options
PRIORITIES = ["Critical", "High", "Medium", "Low"]

# Owner options
OWNERS = ["Kurtis", "Anakin", "External"]

# Phase colors
PHASE_COLORS = {
    "Planning": "blue",
    "Research": "purple",
    "Dev": "green",
    "Testing": "yellow",
    "Deploy": "orange",
    "Monitor": "red",
}

# Status colors
STATUS_COLORS = {
    "Not Started": "default",
    "In Progress": "blue",
    "Blocked": "red",
    "Done": "green",
}

# Priority colors
PRIORITY_COLORS = {
    "Critical": "red",
    "High": "orange",
    "Medium": "yellow",
    "Low": "gray",
}

# Owner colors
OWNER_COLORS = {
    "Kurtis": "blue",
    "Anakin": "purple",
    "External": "default",
}

# Pre-populated tasks
TASKS = [
    {
        "name": "Gather Trustate API docs",
        "phase": "Research",
        "status": "Not Started",
        "priority": "Critical",
        "owner": "Kurtis",
        "blockers": "",
        "dependencies": "",
        "notes": "Collect all API documentation, endpoints, auth flows, and rate limits from Trustate.",
    },
    {
        "name": "Design workflow",
        "phase": "Planning",
        "status": "Not Started",
        "priority": "Critical",
        "owner": "Kurtis",
        "blockers": "",
        "dependencies": "Gather Trustate API docs",
        "notes": "Map out the full automation pipeline from GHL trigger through petition generation to filing.",
    },
    {
        "name": "Identify court filing integrations",
        "phase": "Research",
        "status": "Not Started",
        "priority": "High",
        "owner": "Anakin",
        "blockers": "",
        "dependencies": "",
        "notes": "Research available court e-filing APIs and integration options by jurisdiction.",
    },
    {
        "name": "Build GHL trigger",
        "phase": "Dev",
        "status": "Not Started",
        "priority": "High",
        "owner": "Anakin",
        "blockers": "",
        "dependencies": "Design workflow",
        "notes": "Set up GoHighLevel webhook/trigger to kick off the automation pipeline.",
    },
    {
        "name": "Build petition auto-generation",
        "phase": "Dev",
        "status": "Not Started",
        "priority": "Critical",
        "owner": "Anakin",
        "blockers": "",
        "dependencies": "Design workflow, Gather Trustate API docs",
        "notes": "Implement automated petition document generation from client data.",
    },
    {
        "name": "Build Trustate integration",
        "phase": "Dev",
        "status": "Not Started",
        "priority": "Critical",
        "owner": "Anakin",
        "blockers": "",
        "dependencies": "Gather Trustate API docs, Build petition auto-generation",
        "notes": "Connect the pipeline to Trustate API for trust creation and management.",
    },
    {
        "name": "End-to-end testing",
        "phase": "Testing",
        "status": "Not Started",
        "priority": "High",
        "owner": "Kurtis",
        "blockers": "",
        "dependencies": "Build GHL trigger, Build petition auto-generation, Build Trustate integration",
        "notes": "Full pipeline test with sample data across all integrations.",
    },
    {
        "name": "Pilot deployment",
        "phase": "Deploy",
        "status": "Not Started",
        "priority": "High",
        "owner": "Kurtis",
        "blockers": "",
        "dependencies": "End-to-end testing",
        "notes": "Deploy to production with a small set of pilot clients.",
    },
    {
        "name": "Monitoring & iteration",
        "phase": "Monitor",
        "status": "Not Started",
        "priority": "Medium",
        "owner": "Kurtis",
        "blockers": "",
        "dependencies": "Pilot deployment",
        "notes": "Track success rates, error logs, and client feedback. Iterate on issues.",
    },
]


def notion_request(method, endpoint, token, data=None):
    """Make a request to the Notion API."""
    url = f"{NOTION_BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }

    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"Notion API error ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)


def create_database(token, parent_page_id=None):
    """Create the project tracker database in Notion."""
    # Build parent reference
    if parent_page_id:
        parent = {"type": "page_id", "page_id": parent_page_id}
    else:
        # Use the first page the integration has access to
        results = notion_request("POST", "/search", token, {
            "filter": {"value": "page", "property": "object"},
            "page_size": 1,
        })
        if not results.get("results"):
            print("Error: No accessible pages found. Share a page with your integration first.", file=sys.stderr)
            sys.exit(1)
        parent = {"type": "page_id", "page_id": results["results"][0]["id"]}

    db_schema = {
        "parent": parent,
        "title": [{"type": "text", "text": {"content": "Automation Pipeline Tracker"}}],
        "properties": {
            "Name": {"title": {}},
            "Phase": {
                "select": {
                    "options": [
                        {"name": phase, "color": PHASE_COLORS[phase]}
                        for phase in PHASES
                    ]
                }
            },
            "Status": {
                "select": {
                    "options": [
                        {"name": status, "color": STATUS_COLORS[status]}
                        for status in STATUSES
                    ]
                }
            },
            "Priority": {
                "select": {
                    "options": [
                        {"name": p, "color": PRIORITY_COLORS[p]}
                        for p in PRIORITIES
                    ]
                }
            },
            "Owner": {
                "select": {
                    "options": [
                        {"name": o, "color": OWNER_COLORS[o]}
                        for o in OWNERS
                    ]
                }
            },
            "Blockers": {"rich_text": {}},
            "Dependencies": {"rich_text": {}},
            "Target Date": {"date": {}},
            "Notes": {"rich_text": {}},
        },
    }

    db = notion_request("POST", "/databases", token, db_schema)
    print(f"Created database: {db['url']}")
    return db["id"]


def add_task(token, database_id, task):
    """Add a single task as a page in the database."""
    properties = {
        "Name": {"title": [{"text": {"content": task["name"]}}]},
        "Phase": {"select": {"name": task["phase"]}},
        "Status": {"select": {"name": task["status"]}},
        "Priority": {"select": {"name": task["priority"]}},
        "Owner": {"select": {"name": task["owner"]}},
    }

    if task.get("blockers"):
        properties["Blockers"] = {"rich_text": [{"text": {"content": task["blockers"]}}]}
    if task.get("dependencies"):
        properties["Dependencies"] = {"rich_text": [{"text": {"content": task["dependencies"]}}]}
    if task.get("target_date"):
        properties["Target Date"] = {"date": {"start": task["target_date"]}}
    if task.get("notes"):
        properties["Notes"] = {"rich_text": [{"text": {"content": task["notes"]}}]}

    page = notion_request("POST", "/pages", token, {
        "parent": {"database_id": database_id},
        "properties": properties,
    })
    print(f"  Added: {task['name']}")
    return page["id"]


def main():
    parser = argparse.ArgumentParser(description="Create a Notion project tracker for the automation pipeline.")
    parser.add_argument(
        "--parent-page",
        help="Notion page ID to create the database under. If omitted, uses the first accessible page.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NOTION_API_KEY"),
        help="Notion API token (defaults to NOTION_API_KEY env var).",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: Provide a Notion API token via --token or NOTION_API_KEY env var.", file=sys.stderr)
        sys.exit(1)

    print("Creating Automation Pipeline Tracker database...")
    db_id = create_database(args.token, args.parent_page)

    print("Adding tasks...")
    for task in TASKS:
        add_task(args.token, db_id, task)

    print(f"\nDone! {len(TASKS)} tasks created.")
    print("Open your Notion workspace to see the tracker.")


if __name__ == "__main__":
    main()
