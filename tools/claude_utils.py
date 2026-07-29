import glob
import json
from datetime import datetime
from pathlib import Path

from constants import MONITOR_SERVICE_NAME
from core.logging import get_logger


def get_project_name(path: Path) -> str:
    """Convert path into Claude Code's project-folder name.

    Claude Code names ~/.claude/projects/<name> after the process working
    directory, replacing both "/" and "_" with "-".
    """
    return str(path.resolve()).replace("/", "-").replace("_", "-")


def get_claude_log_path() -> Path:
    """Locate the Claude project transcript dir for the CURRENT process.
    Derive from the process CWD (what every caller passes as
    ``cwd=str(REPO_ROOT)``).
    """
    return Path.home() / ".claude" / "projects" / get_project_name(Path.cwd())


def _get_file_time(file_path):
    # Hypothetically, the latest updated file is the current conversation
    # The logic might fail with subagents
    # Returns a stat_result object containing file metadata
    file_stat = file_path.stat()

    # Fallback mechanism for Unix/Mac/Windows systems
    try:
        timestamp = file_stat.st_birthtime  # Works on macOS and some Linux
    except AttributeError:
        timestamp = file_stat.st_ctime  # Creation on Windows, metadata change on Linux

    readable_time = datetime.fromtimestamp(timestamp)
    return readable_time


def read_jsonl(path):
    data = []
    broken = []
    with open(path, "r") as file:
        for line in file:
            try:
                data.append(json.loads(line))
            except:
                broken.append(line)
    return data, broken


def check_error_last_log(logs) -> str | None:
    logs = logs[::-1]
    for log in logs:
        log_type = log.get("type", "unknown")
        if log_type == "assistant":
            return get_api_error(log)


def get_api_error(log) -> str | None:
    messages = log.get("message", None)
    content = messages.get("content", None) if messages else None

    is_api_error_message = log.get("isApiErrorMessage", None)
    if is_api_error_message:
        return is_api_error_message
    if messages:
        content = messages.get("content", None)
        if messages.get("role") == "assistant":
            if (
                "API Error: Connection closed mid-response. The response above may be incomplete."
                in str(content)
            ):
                return content


def get_api_error_in_session(session_name, service) -> str | None:
    """Check if the last assistant message in the session logs indicates an API error.

    ``service`` should be the caller's own service name (e.g. MONITOR_SERVICE_NAME
    or ORCHESTRATOR_SERVICE_NAME) so log lines are tagged with whoever called in.
    """
    logger = get_logger(service)
    claude_log_path = get_claude_log_path()
    logger.info(f"Checking for Claude project logs in {claude_log_path}")
    transcript_glob = str(claude_log_path / "*.jsonl")
    transcripts = sorted(
        glob.glob(transcript_glob), key=lambda x: _get_file_time(Path(x))
    )
    for file in transcripts:
        logger.info(f"Checking {file} for API error in session '{session_name}'")
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
                        logs, failed_lines = read_jsonl(file)
                        logger.warning(
                            "The following claude project log lines failed to parse as JSON:"
                        )
                        for line in failed_lines:
                            logger.warning(line.strip())
                        error = check_error_last_log(logs)
                        if error:
                            logger.error(
                                f"API error found in session '{session_name}': {error}"
                            )
                            return error
                        else:
                            logger.info(
                                f"No API error found in session '{session_name}'"
                            )
                except json.JSONDecodeError:
                    logger.warning(
                        f"Failed to decode JSON line in claude project logs: {file}: {line.strip()}"
                    )
