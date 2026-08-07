import json
import logging
import os
import shutil
import subprocess
import uuid
from functools import partial
from pathlib import Path

from pydantic import BaseModel

from ninja.agent_providers.base import AgentProvider, AgentRunConfig, AgentRunResult
from ninja.agent_providers.codex.codex_utils import get_latest_traces_session_id
from ninja.clients.litellm_client import get_config
from ninja.clients.super_ninja_client import get_thread_id
from ninja.constants import (
    AGENTS_MD_CODEX_PATH,
    ENV_VAR_CONVERSATION_ID,
    ENV_VAR_FEATURE,
    ENV_VAR_TASK_ID,
    HEADER_NINJA_CONVERSATION_ID,
    HEADER_NINJA_FEATURE,
    HEADER_NINJA_TASK_ID,
)
from ninja.core.config import load_codex_settings, save_codex_settings
from ninja.core.metadata import get_selected_model
from ninja.utils.cost import build_feature, compute_cost

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_CONFIG_DIR / "config.toml"


class CodexUsage(BaseModel):
    """Token usage from Codex. Supports ``+=`` for aggregating across turns."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __iadd__(self, other: "CodexUsage") -> "CodexUsage":
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        return self

    def compute_cost(self, model: str) -> float:
        uncached_input = max(self.input_tokens - self.cached_input_tokens, 0)
        return compute_cost(
            model, uncached_input, self.output_tokens, 0, 0, self.cached_input_tokens
        )


def parse_codex_output(stdout: str) -> CodexUsage:
    """Parse Codex ``--json`` JSONL stream and return aggregated usage."""
    total = CodexUsage()
    if not stdout:
        return total
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        total += CodexUsage.model_validate(usage)
    return total


class CodexProvider(AgentProvider):
    name = "codex"

    def setup(self, logger: logging.Logger) -> bool:
        """
        Setup codex cli and setup skills.
        """
        if not shutil.which("codex"):
            logger.error("Codex CLI not found on PATH!")
            logger.error("Install with: npm install -g @openai/codex")
            raise RuntimeError("Codex CLI not found on PATH")
        self._write_config(logger)
        self._install_skills(logger)
        self._remove_codex_builtin_skills(logger=logger)
        load_codex_settings()

    def upgrade(self, logger: logging.Logger, timeout: int = 120) -> None:
        if not shutil.which("codex"):
            return
        try:
            subprocess.run(
                ["npm", "install", "-g", "@openai/codex@latest"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("codex upgrade skipped", exc_info=True)

    # TODO: move inline config to a config template
    def _write_config(self, logger: logging.Logger) -> None:
        """Codex uses TOML. Point it at LiteLLM via an OpenAI-compatible
        provider so it reuses the same gateway/token Claude uses."""
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        base_url = get_config().get("base_url", "")
        model = get_selected_model()
        stop_hook = REPO_ROOT / "agent_providers" / "codex_hooks" / "codex_stop_hook.py"
        config = f"""\
model = "{model}"
model_provider = "litellm"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_auto_compact_token_limit = 200000

[tools]
view_image = false
web_search = false

[model_providers.litellm]
name = "litellm"
base_url = "{base_url}"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
env_http_headers = {{ "{HEADER_NINJA_TASK_ID}" = "{ENV_VAR_TASK_ID}", "{HEADER_NINJA_FEATURE}" = "{ENV_VAR_FEATURE}", "{HEADER_NINJA_CONVERSATION_ID}" = "{ENV_VAR_CONVERSATION_ID}" }}

[projects."/workspace/ninja"]
trust_level = "trusted"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = 'python3 {stop_hook}'
timeout = 60
"""
        CODEX_CONFIG_FILE.write_text(config, encoding="utf-8")
        logger.info("Wrote %s", CODEX_CONFIG_FILE)

    def _install_skills(self, logger: logging.Logger) -> None:
        """Codex skills"""
        skills_parent = REPO_ROOT / ".codex"
        skills_root = skills_parent / "skills"
        if not skills_root.exists():
            subprocess.run(
                [
                    "unzip",
                    "-o",
                    str(REPO_ROOT / "skills.zip"),
                    "-d",
                    str(skills_parent),
                    "-x",
                    "__MACOSX/*",
                    "*/.DS_Store",
                ],
                capture_output=True,
                text=True,
            )
        lines = [
            "## Available Skills",
            "File-based skills — read the SKILL.md then run its scripts.",
            "",
        ]
        logger.info(
            f"Found {len(list(skills_root.glob('*/SKILL.md')))} skills in {skills_root}"
        )
        for skill_md in sorted(skills_root.glob("*/SKILL.md")):
            desc = ""
            for ln in skill_md.read_text(encoding="utf-8").splitlines():
                if ln.startswith("description:"):
                    desc = ln[len("description:") :].strip()
                    break
            rel = skill_md.relative_to(REPO_ROOT)
            lines.append(f"- `{skill_md.parent.name}` — {desc} (see `{rel}`)")
        AGENTS_MD_CODEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _remove_codex_builtin_skills(*, logger: logging.Logger) -> None:
        """Remove Codex CLI bundled skills from ~/.codex/skills/.system."""
        system_skills_dir = CODEX_CONFIG_DIR / "skills" / ".system"
        if system_skills_dir.is_dir():
            shutil.rmtree(system_skills_dir)
            logger.info("Removed Codex bundled system skills at %s", system_skills_dir)

    @staticmethod
    def _resume_failed(r: subprocess.CompletedProcess) -> bool:
        if r.returncode == 0:
            return False
        combined = ((r.stdout or "") + (r.stderr or "")).lower()
        return "session" in combined or "not found" in combined

    def run(self, spec: AgentRunConfig, logger: logging.Logger) -> AgentRunResult:
        """Run one invocation of the Codex CLI and return a normalised result."""
        logger.info("Codex: starting run with %ds timeout", spec.timeout_seconds)

        prompt_file = self._write_prompt_file(spec.prompt)
        try:
            cfg = get_config()
            auth_token = cfg["api_key"]
            if not auth_token:
                logger.error(
                    "ANTHROPIC_AUTH_TOKEN not found in settings.json "
                    f"(looked in source: {cfg.get('source') or 'no settings file'})"
                )

            task_id = str(uuid.uuid4())
            conversation_id = get_thread_id() or str(uuid.uuid4())
            env = {
                **os.environ,
                **spec.env,
                "LITELLM_API_KEY": auth_token,
                ENV_VAR_TASK_ID: task_id,
                ENV_VAR_FEATURE: build_feature(spec.title),
                ENV_VAR_CONVERSATION_ID: conversation_id,
            }
            logger.info(
                f"Codex running with task_id: {task_id}, conversation_id: {conversation_id}"
            )

            run_codex = partial(
                subprocess.run,
                cwd=str(spec.cwd or REPO_ROOT),
                timeout=spec.timeout_seconds + 60,
                capture_output=True,
                text=True,
                env=env,
            )

            session_id = (
                load_codex_settings().get("session_id", {}).get(spec.session_name)
            )
            logger.info(f"Using session_id for {spec.session_name}: {session_id}")

            argv = self.get_argv(spec, session_id)
            with open(prompt_file, encoding="utf-8") as stdin_f:
                r = run_codex(argv, stdin=stdin_f)

            if session_id and self._resume_failed(r):
                logger.warning(f"Codex resume failed for {session_id}; retrying fresh")
                save_codex_settings(spec.session_name, None)
                argv = self.get_argv(spec, None)
                with open(prompt_file, encoding="utf-8") as stdin_f:
                    r = run_codex(argv, stdin=stdin_f)
                session_id = None

            if not session_id:
                session_id = get_latest_traces_session_id()
                if session_id:
                    logger.info(
                        f"Discovered session id for {spec.session_name}: {session_id}"
                    )
                    save_codex_settings(spec.session_name, session_id)

            res = AgentRunResult(r.stdout, r.stderr, r.returncode)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Codex CLI timed out after %d minutes",
                (spec.timeout_seconds + 60) // 60,
            )
            res = AgentRunResult(timed_out=True, exit_code=124)
        except OSError as exc:
            logger.error(f"OS error running Codex: {exc}")
            res = AgentRunResult(stderr=str(exc), exit_code=1)
        finally:
            os.unlink(prompt_file)

        return res

    def get_argv(self, spec: AgentRunConfig, session_id: str | None) -> list[str]:
        """Return the argv for a Codex run"""
        argv = ["codex", "exec"]
        if session_id:
            argv += ["resume", session_id]
        argv.append("--skip-git-repo-check")
        argv.append("--json")
        if not session_id:
            argv += ["--cd", str(spec.cwd or REPO_ROOT)]
        if spec.system_prompt_enabled and spec.system_prompt_path:
            argv += [
                "-c",
                f'model_instructions_file="{spec.system_prompt_path}"',
            ]
        argv.append("-")
        return argv
