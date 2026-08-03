import logging
import os
import shutil
import subprocess
from pathlib import Path

from ninja.agent_providers.base import AgentProvider, AgentRunConfig, AgentRunResult
from ninja.agent_providers.codex.codex_utils import get_latest_traces_session_id
from ninja.clients.litellm_client import get_config
from ninja.constants import AGENTS_MD_CODEX_PATH
from ninja.core.config import load_codex_settings, save_codex_settings
from ninja.core.metadata import get_selected_model

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_CONFIG_DIR / "config.toml"


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

    def _write_config(self, logger: logging.Logger) -> None:
        """Codex uses TOML. Point it at LiteLLM via an OpenAI-compatible
        provider so it reuses the same gateway/token Claude uses."""
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        base_url = get_config().get("base_url", "")
        model = get_selected_model()

        config = f"""\
model = "{model}"
model_provider = "litellm"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.litellm]
name = "litellm"
base_url = "{base_url}"
env_key = "LITELLM_API_KEY"
wire_api = "responses"

[projects."/workspace/ninja"]
trust_level = "trusted"
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

            env = {
                **os.environ,
                **spec.env,
                "LITELLM_API_KEY": auth_token,
            }

            codex_settings = load_codex_settings()
            session_id = codex_settings.get("session_id", {}).get(spec.session_name)
            logger.info(f"Using session_id for {spec.session_name}: {session_id}")
            argv = self.get_argv(spec, session_id)

            with open(prompt_file, encoding="utf-8") as stdin_f:
                r = subprocess.run(
                    argv,
                    stdin=stdin_f,
                    cwd=str(spec.cwd or REPO_ROOT),
                    timeout=spec.timeout_seconds + 60,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            # Set session id for each service if not already
            # The reason it uses the latest trace/log session to set the session id is based on the assumption that
            # the latest session is the one that is currently running.
            # This assumption fails in the case where orcherstrator runs longer than monitor
            if not session_id:
                session_id = get_latest_traces_session_id()
                if session_id:
                    logger.info(
                        f"Latest trace session id for {spec.session_name}: {session_id}"
                    )
                    if (
                        codex_settings.get("session_id", {}).get(spec.session_name)
                        == None
                    ):
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
        if not session_id:
            argv += ["--cd", str(spec.cwd or REPO_ROOT)]
        if spec.system_prompt_enabled and spec.system_prompt_path:
            argv += [
                "-c",
                f'model_instructions_file="{spec.system_prompt_path}"',
            ]
        argv.append("-")
        return argv
