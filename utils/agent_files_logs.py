import json
from pathlib import Path


def get_logs_files():
    claude_projects = Path.home() / ".claude" / "projects"
    return claude_projects.rglob("*.jsonl")


def if_session_exists_by_name(session_name):
    log_files = get_logs_files()
    for file in log_files:
        with open(file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if (
                        entry.get("type") == "custom-title"
                        and entry.get("customTitle") == session_name
                    ) or (
                        entry.get("type") == "agent-name"
                        and entry.get("agentName") == session_name
                    ):
                        return True
                except json.JSONDecodeError:
                    pass
    return False
