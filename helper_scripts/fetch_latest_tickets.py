"""Fetch latest 40 completed JIRA tickets from IDRE project."""
import requests
from requests.auth import HTTPBasicAuth
import json
import os
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).parent.parent  # Go up to final folder
load_dotenv(HERE / ".env")

jira_base = os.getenv("JIRA_URL", "").rstrip("/")
username = os.getenv("JIRA_USERNAME")
token = os.getenv("JIRA_API_TOKEN")

# Fetch latest 40 completed tickets
jql = 'project = IDRE AND status in (Done, "Code Review", Testing, "Ready for Deployment") ORDER BY updated DESC'
url = f"{jira_base}/rest/api/3/search/jql"  # New endpoint (old /search is deprecated)
params = {"jql": jql, "maxResults": 40, "fields": "summary,status,assignee"}

print(f"Fetching from: {url}")
print(f"JQL: {jql}\n")

r = requests.get(url, params=params, auth=HTTPBasicAuth(username, token), timeout=30)
r.raise_for_status()
data = r.json()

print(f"Found {len(data['issues'])} completed tickets:\n")
tickets = []
for issue in data["issues"]:
    key = issue["key"]
    summary = issue["fields"]["summary"]
    status = issue["fields"]["status"]["name"]
    assignee = issue["fields"].get("assignee", {})
    assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
    tickets.append(key)
    print(f"{key} | {status:20s} | {assignee_name:25s} | {summary[:60]}")

print("\n" + "="*80)
print("Python list format for multi_model_benchmark.py:")
print("="*80)
print("TARGET_JIRA_IDS = [")
for i in range(0, len(tickets), 5):
    chunk = tickets[i:i+5]
    line = ", ".join(f'"{t}"' for t in chunk)
    print(f"    {line},")
print("]")
print(f"\nTotal: {len(tickets)} tickets")
