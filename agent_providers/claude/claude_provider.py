"""Claude Code agent provider."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path

from ninja.agent_providers.base import AgentProvider, AgentRunConfig, AgentRunResult
from ninja.agent_providers.claude.claude_output import parse_claude_output

_FIX_JSONL = Path(__file__).parent / "fix_jsonl.sh"
_WRAPPER = Path(__file__).parent / "claude-wrapper.sh"

_RESUME_FAIL_PATTERNS = ("No conversation found", "Failed to resume")
_API_ERROR_PATTERNS = (
    "API Error: Connection closed mid-response",
    "overloaded_error",
    "rate_limit_error",
    "authentication_error",
    "billing_error",
)


def resume_failed(stderr: str | None) -> bool:
    if not stderr:
        return False
    return any(pat in stderr for pat in _RESUME_FAIL_PATTERNS)


def detect_api_error(result) -> str | None:
    """Check stderr and exit code for API errors."""
    stderr = result.stderr or ""
    for pat in _API_ERROR_PATTERNS:
        if pat in stderr:
            return stderr.strip()
    exit_code = getattr(result, "returncode", None) or getattr(result, "exit_code", 0)
    if exit_code not in (0, 124) and stderr.strip() and not resume_failed(stderr):
        return f"Claude exited {exit_code}: {stderr[:500]}"
    return None


# ---------------------------------------------------------------------------
# Session ID persistence (~/.claude_state/<role>.id)
# ---------------------------------------------------------------------------
class SessionStore:
    """Manages session ID files under ``~/.claude_state/``."""

    def __init__(self, state_dir: Path | None = None):
        self._dir = state_dir or Path.home() / ".claude_state"
        self._dir_exists = False

    def _ensure_dir(self) -> Path:
        if not self._dir_exists:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._dir_exists = True
        return self._dir

    def _path(self, role: str) -> Path:
        safe = re.sub(r"[^\w\-]", "_", role)
        return self._ensure_dir() / f"{safe}.id"

    def _backup(self, path: Path) -> None:
        if not path.is_file():
            return
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        try:
            path.rename(path.with_suffix(f".{ts}.bak"))
        except OSError:
            pass

    def build_args(
        self, role: str, preferred_id: str | None = None
    ) -> tuple[list[str], str, bool]:
        """Return ``([cli_args], session_uuid, is_new)``."""
        path = self._path(role)
        if path.is_file():
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                return ["--resume", stored], stored, False
        sid = preferred_id or str(uuid.uuid4())
        return ["--session-id", sid, "-n", role], sid, True

    def mark_created(self, role: str, sid: str) -> None:
        path = self._path(role)
        if path.is_file() and path.read_text(encoding="utf-8").strip() == sid:
            return
        self._backup(path)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            os.write(fd, (sid + "\n").encode())
            os.close(fd)
            os.rename(tmp, str(path))
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def forget(self, role: str) -> None:
        path = self._path(role)
        self._backup(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


_sessions = SessionStore()

# Module-level convenience aliases (used by callers and tests).
build_session_args = _sessions.build_args
mark_created = _sessions.mark_created
forget = _sessions.forget


def get_project_name(cwd: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))


def get_transcript_path(sid: str, cwd: Path) -> Path | None:
    path = Path.home() / ".claude" / "projects" / get_project_name(cwd) / f"{sid}.jsonl"
    return path if path.is_file() else None


def attempt_repair(sid: str, cwd: Path) -> bool:
    transcript = get_transcript_path(sid, cwd)
    if not transcript or not _FIX_JSONL.is_file():
        return False
    try:
        r = subprocess.run(
            ["bash", str(_FIX_JSONL), "-i", str(transcript)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class ClaudeProvider(AgentProvider):
    name = "claude"

    def setup(self, logger: logging.Logger) -> bool:
        if not shutil.which("claude"):
            logger.error("Claude CLI not found on PATH")
            raise RuntimeError("Claude CLI not found on PATH")
        return True

    def upgrade(self, logger: logging.Logger, timeout: int = 60) -> None:
        if not shutil.which("claude"):
            logger.debug("claude CLI not on PATH — skipping upgrade")
            return
        before = self._cli_version()
        try:
            r = subprocess.run(
                ["claude", "update"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"claude update timed out after {timeout}s")
            return
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"claude update failed to start ({exc})")
            return
        if r.returncode != 0:
            logger.warning(
                f"claude update exited {r.returncode}: "
                f"{(r.stderr or r.stdout).strip()[:200]}"
            )
            return
        after = self._cli_version()
        if before and after and before != after:
            logger.info(f"Upgraded Claude CLI {before} -> {after}")
        elif after:
            logger.info(f"Claude CLI is up to date ({after})")

    def run(self, spec: AgentRunConfig, logger: logging.Logger) -> AgentRunResult:
        cwd = str(spec.cwd or Path.cwd())
        prompt_file = self._write_prompt_file(spec.prompt)
        try:
            session_args, sid, is_new = build_session_args(spec.session_name)
            env = {
                **os.environ,
                **spec.env,
                "CLAUDE_PROMPT_FILE": prompt_file,
                "CLAUDE_TIMEOUT": str(spec.timeout_seconds),
                "CLAUDE_CWD": cwd,
            }
            python_timeout = spec.timeout_seconds + 60

            run_claude = partial(
                subprocess.run,
                cwd=cwd,
                timeout=python_timeout,
                capture_output=True,
                text=True,
                env=env,
            )

            cli_args = self._build_cli_args(spec, session_args)
            r = run_claude(cli_args)

            if r.returncode == 0:
                mark_created(spec.session_name, sid)
            elif not is_new and resume_failed(r.stderr):
                logger.warning(f"Session resume failed for {sid}; attempting repair")
                if attempt_repair(sid, Path(cwd)):
                    logger.info("Transcript repaired; retrying --resume")
                    r = run_claude(cli_args)
                if resume_failed(r.stderr):
                    logger.warning("Creating fresh session")
                    forget(spec.session_name)
                    session_args, sid, _ = build_session_args(spec.session_name)
                    cli_args = self._build_cli_args(spec, session_args)
                    r = run_claude(cli_args)
                    if r.returncode == 0:
                        mark_created(spec.session_name, sid)

            parsed = parse_claude_output(r.stdout)
            return AgentRunResult(
                stdout=r.stdout,
                stderr=r.stderr,
                exit_code=r.returncode,
                parsed=parsed,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"Claude CLI timed out after {(spec.timeout_seconds + 60) // 60} min"
            )
            return AgentRunResult(timed_out=True, exit_code=124)
        except OSError as exc:
            logger.error(f"OS error running Claude: {exc}")
            return AgentRunResult(stderr=str(exc), exit_code=1)
        finally:
            os.unlink(prompt_file)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _build_cli_args(
        self, spec: AgentRunConfig, session_args: list[str]
    ) -> list[str]:
        args = [str(_WRAPPER), *session_args]
        if spec.system_prompt_enabled and spec.system_prompt_path:
            flag = (
                "--append-system-prompt-file"
                if spec.system_prompt_mode == "append"
                else "--system-prompt-file"
            )
            args += [flag, str(spec.system_prompt_path)]
            if spec.tools:
                args += ["--tools", ",".join(spec.tools)]
        args += ["--output-format", "json", "-p"]
        return args

    @staticmethod
    def _cli_version() -> str:
        try:
            r = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().split()[0]
        except (subprocess.SubprocessError, OSError):
            pass
        return ""
