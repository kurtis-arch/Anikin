#!/usr/bin/env python3
"""
Notion Workspace Setup for Kurtis
===================================
Creates databases, populates records, sets up relations and views.

Requirements:
    pip install requests

Usage:
    python setup_notion_workspace.py --token <NOTION_TOKEN> --page <PARENT_PAGE_ID>

The parent page must already exist in Notion and the integration must be
connected to it (Page ... > Connections > Add your integration).
"""

import argparse
import json
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Error: 'requests' package required. Install with: pip install requests")

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_request(method: str, endpoint: str, token: str, payload: dict = None, retries: int = 3):
    """Make a Notion API request with retry logic."""
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries):
        resp = getattr(requests, method)(url, headers=headers(token), json=payload)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            print(f"  Error {resp.status_code}: {resp.text}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
        return resp.json()
    return None


def rich_text(content: str) -> list:
    return [{"type": "text", "text": {"content": content}}]


def title_prop(content: str) -> dict:
    return {"title": rich_text(content)}


def select_prop(name: str) -> dict:
    return {"select": {"name": name}}


def date_prop(start: str) -> dict:
    return {"date": {"start": start}}


def rich_text_prop(content: str) -> dict:
    return {"rich_text": rich_text(content)}


def relation_prop(page_ids: list) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


# ---------------------------------------------------------------------------
# Database creation
# ---------------------------------------------------------------------------

def create_crons_db(token: str, parent_id: str) -> str:
    print("Creating Crons database...")
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "\u23f0"},
        "title": rich_text("Crons"),
        "properties": {
            "Name": {"title": {}},
            "Schedule": {"rich_text": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Active", "color": "green"},
                        {"name": "Paused", "color": "yellow"},
                        {"name": "Completed", "color": "blue"},
                    ]
                }
            },
            "Last Run": {"date": {}},
            "Next Run": {"date": {}},
            "Notes": {"rich_text": {}},
        },
    }
    result = notion_request("post", "/databases", token, payload)
    db_id = result["id"]
    print(f"  Created Crons DB: {db_id}")
    return db_id


def create_projects_db(token: str, parent_id: str) -> str:
    print("Creating Projects database...")
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4c1"},
        "title": rich_text("Projects"),
        "properties": {
            "Project Name": {"title": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Active", "color": "green"},
                        {"name": "In Progress", "color": "blue"},
                        {"name": "Completed", "color": "gray"},
                        {"name": "On Hold", "color": "yellow"},
                    ]
                }
            },
            "Start Date": {"date": {}},
            "Target Completion": {"date": {}},
            "Description": {"rich_text": {}},
        },
    }
    result = notion_request("post", "/databases", token, payload)
    db_id = result["id"]
    print(f"  Created Projects DB: {db_id}")
    return db_id


def create_tasks_db(token: str, parent_id: str, projects_db_id: str) -> str:
    print("Creating Tasks database...")
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "\u2705"},
        "title": rich_text("Tasks"),
        "properties": {
            "Task Name": {"title": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "To Do", "color": "red"},
                        {"name": "In Progress", "color": "yellow"},
                        {"name": "Done", "color": "green"},
                    ]
                }
            },
            "Priority": {
                "select": {
                    "options": [
                        {"name": "High", "color": "red"},
                        {"name": "Medium", "color": "yellow"},
                        {"name": "Low", "color": "green"},
                    ]
                }
            },
            "Due Date": {"date": {}},
            "Assignee": {"rich_text": {}},
            "Project": {
                "relation": {
                    "database_id": projects_db_id,
                    "single_property": {},
                }
            },
            "Notes": {"rich_text": {}},
        },
    }
    result = notion_request("post", "/databases", token, payload)
    db_id = result["id"]
    print(f"  Created Tasks DB: {db_id}")
    return db_id


def create_pending_items_db(token: str, parent_id: str) -> str:
    print("Creating Pending Items database...")
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "\u23f3"},
        "title": rich_text("Pending Items"),
        "properties": {
            "Item": {"title": {}},
            "Waiting For": {"rich_text": {}},
            "From": {"rich_text": {}},
            "Target Completion": {"date": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Waiting", "color": "yellow"},
                        {"name": "In Progress", "color": "blue"},
                        {"name": "Resolved", "color": "green"},
                    ]
                }
            },
        },
    }
    result = notion_request("post", "/databases", token, payload)
    db_id = result["id"]
    print(f"  Created Pending Items DB: {db_id}")
    return db_id


# ---------------------------------------------------------------------------
# Add relation from Projects back to Tasks (bidirectional)
# ---------------------------------------------------------------------------

def add_tasks_relation_to_projects(token: str, projects_db_id: str, tasks_db_id: str):
    """Update Projects DB to add a relation pointing to Tasks DB."""
    print("Adding Tasks relation to Projects database...")
    payload = {
        "properties": {
            "Tasks": {
                "relation": {
                    "database_id": tasks_db_id,
                    "single_property": {},
                }
            }
        }
    }
    notion_request("patch", f"/databases/{projects_db_id}", token, payload)
    print("  Linked Projects <-> Tasks")


# ---------------------------------------------------------------------------
# Record population
# ---------------------------------------------------------------------------

def add_page(token: str, db_id: str, properties: dict):
    payload = {
        "parent": {"database_id": db_id},
        "properties": properties,
    }
    return notion_request("post", "/pages", token, payload)


def populate_crons(token: str, db_id: str):
    print("Populating Crons records...")
    crons = [
        {
            "Name": title_prop("Closer Alert Cron — Eastern"),
            "Schedule": rich_text_prop("Mon-Fri 6:15 PM ET"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Closer alert for Eastern timezone reps"),
        },
        {
            "Name": title_prop("Closer Alert Cron — Central"),
            "Schedule": rich_text_prop("Mon-Fri 6:15 PM CT"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Closer alert for Central timezone reps"),
        },
        {
            "Name": title_prop("Closer Alert Cron — Mountain"),
            "Schedule": rich_text_prop("Mon-Fri 6:15 PM MT"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Closer alert for Mountain timezone reps"),
        },
        {
            "Name": title_prop("Google Drive Sync"),
            "Schedule": rich_text_prop("Daily (every 24h)"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("AGENTS.md failing — fix needed"),
        },
        {
            "Name": title_prop("Morning Briefing"),
            "Schedule": rich_text_prop("6:00 AM UTC (8:00 AM Spain)"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Daily morning briefing delivery"),
        },
        {
            "Name": title_prop("ZipRecruiter Screener — Morning"),
            "Schedule": rich_text_prop("8:00 AM ET"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Morning candidate screening run"),
        },
        {
            "Name": title_prop("ZipRecruiter Screener — Noon"),
            "Schedule": rich_text_prop("12:00 PM ET"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Midday candidate screening run"),
        },
        {
            "Name": title_prop("Call Center Report"),
            "Schedule": rich_text_prop("See HEARTBEAT.md for schedule details"),
            "Status": select_prop("Active"),
            "Notes": rich_text_prop("Teledirect call center reporting — details in HEARTBEAT.md"),
        },
    ]
    for cron in crons:
        add_page(token, db_id, cron)
        print(f"  Added: {cron['Name']['title'][0]['text']['content']}")


def populate_pending_items(token: str, db_id: str):
    print("Populating Pending Items records...")
    items = [
        {
            "Item": title_prop("AGENTS.md YAML fix"),
            "Waiting For": rich_text_prop("YAML configuration fix"),
            "From": rich_text_prop("Jarvis"),
            "Status": select_prop("Waiting"),
        },
        {
            "Item": title_prop("Samantha email template update"),
            "Waiting For": rich_text_prop("Email template revision"),
            "From": rich_text_prop("Samantha"),
            "Status": select_prop("Waiting"),
        },
        {
            "Item": title_prop("Trustate API docs"),
            "Waiting For": rich_text_prop("API documentation delivery"),
            "From": rich_text_prop("Trustate"),
            "Status": select_prop("Waiting"),
        },
    ]
    for item in items:
        add_page(token, db_id, item)
        print(f"  Added: {item['Item']['title'][0]['text']['content']}")


def populate_projects(token: str, db_id: str) -> dict:
    """Populate projects and return a dict of project_name -> page_id."""
    print("Populating Projects records...")
    projects = [
        {
            "name": "Trustate Backoffice Workflow",
            "props": {
                "Project Name": title_prop("Trustate Backoffice Workflow"),
                "Status": select_prop("In Progress"),
                "Description": rich_text_prop(
                    "6-agent paralegal system: Estate Summary, Missing Task Detector, "
                    "Backlog Triage, Document Drafting, Trustate Navigation, Deadline Monitoring"
                ),
            },
        },
        {
            "name": "Aircall Transcripts",
            "props": {
                "Project Name": title_prop("Aircall Transcripts"),
                "Status": select_prop("Active"),
                "Description": rich_text_prop(
                    "Call extraction, storage, analysis by agent/date"
                ),
            },
        },
        {
            "name": "ZipRecruiter Screener",
            "props": {
                "Project Name": title_prop("ZipRecruiter Screener"),
                "Status": select_prop("Active"),
                "Description": rich_text_prop(
                    "Candidate tracking, scoring, auto-invite workflow"
                ),
            },
        },
        {
            "name": "Call Center Management",
            "props": {
                "Project Name": title_prop("Call Center Management"),
                "Status": select_prop("Active"),
                "Description": rich_text_prop(
                    "Teledirect reporting, account monitoring, alerts"
                ),
            },
        },
        {
            "name": "Commission Invoicing",
            "props": {
                "Project Name": title_prop("Commission Invoicing"),
                "Status": select_prop("In Progress"),
                "Description": rich_text_prop(
                    "Template management, drafting, approval"
                ),
            },
        },
        {
            "name": "Google Drive Sync",
            "props": {
                "Project Name": title_prop("Google Drive Sync"),
                "Status": select_prop("In Progress"),
                "Description": rich_text_prop(
                    "File sync, format conversion, error tracking"
                ),
            },
        },
        {
            "name": "Browser Management",
            "props": {
                "Project Name": title_prop("Browser Management"),
                "Status": select_prop("Active"),
                "Description": rich_text_prop(
                    "Anakin browser config, session tracking, data directories"
                ),
            },
        },
    ]
    project_ids = {}
    for proj in projects:
        result = add_page(token, db_id, proj["props"])
        project_ids[proj["name"]] = result["id"]
        print(f"  Added: {proj['name']}")
    return project_ids


# ---------------------------------------------------------------------------
# Views (created as filtered database queries saved in the parent page)
# ---------------------------------------------------------------------------

def create_view_page(token: str, parent_id: str, title: str, db_id: str, description: str):
    """Create a linked database view as a page with an embedded database reference."""
    print(f"Creating view: {title}...")
    # Notion API doesn't support creating views directly, so we create
    # child pages with linked database blocks and descriptive content.
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4ca"},
        "properties": {
            "title": rich_text(title),
        },
        "children": [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "icon": {"type": "emoji", "emoji": "\U0001f4cb"},
                    "rich_text": rich_text(description),
                },
            },
            {
                "object": "block",
                "type": "linked_to_database",
                "linked_to_database": {
                    "database_id": db_id,
                },
            },
        ],
    }
    result = notion_request("post", "/pages", token, payload)
    if result:
        print(f"  Created: {title} -> {result['id']}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Set up Notion workspace for Kurtis")
    parser.add_argument("--token", required=True, help="Notion integration token")
    parser.add_argument("--page", required=True, help="Parent page ID (32-char hex, no dashes)")
    args = parser.parse_args()

    token = args.token
    parent_id = args.page.replace("-", "")

    print("=" * 60)
    print("  NOTION WORKSPACE SETUP FOR KURTIS")
    print("=" * 60)
    print()

    # Verify token
    print("Verifying API access...")
    me = notion_request("get", "/users/me", token)
    if not me:
        sys.exit("Failed to authenticate with Notion API. Check your token.")
    print(f"  Authenticated as: {me.get('name', me.get('bot', {}).get('owner', {}).get('type', 'integration'))}")
    print()

    # --- Step 1: Create databases ---
    print("-" * 40)
    print("STEP 1: Creating Databases")
    print("-" * 40)

    projects_db_id = create_projects_db(token, parent_id)
    crons_db_id = create_crons_db(token, parent_id)
    pending_db_id = create_pending_items_db(token, parent_id)
    tasks_db_id = create_tasks_db(token, parent_id, projects_db_id)

    # Add bidirectional relation
    add_tasks_relation_to_projects(token, projects_db_id, tasks_db_id)
    print()

    # --- Step 2: Populate records ---
    print("-" * 40)
    print("STEP 2: Populating Records")
    print("-" * 40)

    populate_crons(token, crons_db_id)
    print()
    populate_pending_items(token, pending_db_id)
    print()
    project_ids = populate_projects(token, projects_db_id)
    print()

    # --- Step 3: Create view pages ---
    print("-" * 40)
    print("STEP 3: Creating Dashboard Views")
    print("-" * 40)

    create_view_page(
        token, parent_id,
        "Crons Dashboard",
        crons_db_id,
        "Filtered by Status=Active, sorted by Next Run. "
        "Open the linked database below and set filter: Status = Active, sort by Next Run ascending."
    )
    create_view_page(
        token, parent_id,
        "Today's Tasks",
        tasks_db_id,
        "Filtered by Status != Done, sorted by Priority descending. "
        "Open the linked database below and set filter: Status is not Done, sort by Priority descending."
    )
    create_view_page(
        token, parent_id,
        "Project Timeline",
        projects_db_id,
        "Calendar view by Target Completion. "
        "Open the linked database below and switch to Calendar view, set date property to Target Completion."
    )
    create_view_page(
        token, parent_id,
        "Blockers",
        pending_db_id,
        "Sorted by Target Completion ascending. "
        "Open the linked database below and sort by Target Completion ascending."
    )
    print()

    # --- Summary ---
    print("=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print()
    print("Databases created:")
    print(f"  Projects:      {projects_db_id}")
    print(f"  Tasks:         {tasks_db_id}")
    print(f"  Crons:         {crons_db_id}")
    print(f"  Pending Items: {pending_db_id}")
    print()
    print("Projects populated:")
    for name, pid in project_ids.items():
        print(f"  {name}: {pid}")
    print()
    print("Views created as pages with linked databases.")
    print("NOTE: Open each view page and manually set the filter/sort/calendar")
    print("as described in the callout — the Notion API does not support creating")
    print("saved views with filters directly.")
    print()
    print(f"Workspace URL: https://notion.so/{parent_id.replace('-', '')}")
    print()


if __name__ == "__main__":
    main()
