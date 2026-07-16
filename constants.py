from pathlib import Path

HEADER_NINJA_TASK_ID = "x-ninja-task-id"
HEADER_NINJA_CONVERSATION_ID = "x-ninja-conversation-id"
HEADER_NINJA_EVENT_ID = "x-ninja-event-id"
HEADER_NINJA_SANDBOX_ID = "x-ninja-sandbox-id"
HEADER_NINJA_FEATURE = "x-ninja-feature"

LABEL_GENERATE_TASK_TITLE = "Generate Task Title"
DEFAULT_TASK_TITLE = "User prompt"

# Runtime metadata file paths — single source of truth used across the codebase
AGENT_SETTINGS_PATH = Path.home() / ".agent_settings.json"
COST_LIMIT_PATH = Path.home() / ".cost_limit.json"
SANDBOX_METADATA_PATH = Path("/dev/shm/sandbox_metadata.json")
PH_METADATA_PATH = Path("/dev/shm/ph_metadata.json")
ORCHESTRATOR_CONFIG_PATH = Path.home() / ".orchestrator_config.json"

DEFAULT_ORCHESTRATOR_CONFIG = {
    "enabled": True,
    "updated_at": None,
}

SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.txt"
SYSTEM_PROMPT_PATH_SINGLE = Path(__file__).parent / "system_prompt_single.txt"
SYSTEM_PROMPT_PATH_ORCHESTRATOR = (
    Path(__file__).parent / "system_prompt_orchestrator.txt"
)

SYSTEM_PROMPT_FEATURE_FLAG = "system-prompt-phantom"
# Stop-hook chained orchestrator cycles (see orchestrator_stop_hook.py)
STOP_HOOKS_FEATURE_FLAG = "orchestrator-stop-hooks"
