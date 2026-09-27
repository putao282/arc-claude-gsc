"""Official ClaudeSDKClient driver for ARC-Bench contest agent.

Mirrors /workspace/arc-hackathon-eval-v5/official-cc-starter/extract/main.py
(implement_modules_async / claude_env_from_openai_env) while preserving our
STEP loop, GSC MCP, and false-green harness gates in main.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "Bash",
    "Skill",
]

DEFAULT_DISALLOWED_TOOLS = [
    "Agent",
    "Task",
    "WebSearch",
    "WebFetch",
    "CronCreate",
    "CronDelete",
    "CronList",
    "NotebookEdit",
    "EnterWorktree",
    "ExitWorktree",
    "ListAgents",
    "ScheduleWakeup",
    "SendMessage",
    "Workflow",
    "DesignSync",
    "ReportFindings",
]


@dataclass(frozen=True)
class SdkTurnResult:
    """SDK turn outcome mapped to harness ClaudeRunResult fields."""

    returncode: int
    is_error: bool
    terminal_reason: str
    subtype: str
    api_error_status: int | None
    tail: str
    skills_loaded: tuple[str, ...] = ()
    mcp_tools_used: tuple[str, ...] = ()
    driver: str = "ClaudeSDKClient"


@contextlib.contextmanager
def claude_env_from_openai_env() -> Iterator[None]:
    """Preserve the existing ARC-Bench OpenAI-compatible → Claude SDK mapping.

    Exact official starter semantics:
      ANTHROPIC_API_KEY = ""
      ANTHROPIC_BASE_URL = OPENAI_BASE_URL (when set; no /v1 stripping)
      ANTHROPIC_AUTH_TOKEN = OPENAI_API_KEY (when set)
    """
    keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    previous = {key: os.environ.get(key) for key in keys}
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base_url = os.environ.get("OPENAI_BASE_URL", "").strip()

    os.environ["ANTHROPIC_API_KEY"] = ""
    if openai_base_url:
        os.environ["ANTHROPIC_BASE_URL"] = openai_base_url
    else:
        os.environ.pop("ANTHROPIC_BASE_URL", None)
    if openai_key:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = openai_key
    else:
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _require_claude_sdk() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Install claude-agent-sdk before running the ClaudeSDKClient driver."
        ) from exc
    return ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, AssistantMessage, SystemMessage


def gsc_mcp_servers(
    *,
    gsc_dir: Path | None,
    mcp_config: Path | None,
    enable_mcp: bool,
) -> dict[str, Any] | Path | None:
    """Build ClaudeAgentOptions.mcp_servers for GSC.

    Prefer the written mcp-config Path (includes allowedTools). Fall back to an
    inline stdio server dict when only gsc_dir is available.
    """
    if not enable_mcp:
        return None
    if mcp_config is not None and Path(mcp_config).is_file():
        return Path(mcp_config)
    if gsc_dir is None:
        return None
    bootstrap = Path(gsc_dir) / "mcp" / "src" / "bootstrap.mjs"
    if not bootstrap.is_file():
        return None
    return {
        "arch": {
            "type": "stdio",
            "command": "node",
            "args": [str(bootstrap)],
            "env": {
                "CLAUDE_PLUGIN_ROOT": str(gsc_dir),
                "GSC_ARC_PACKAGED_RUNTIME": "1",
                "GSC_RUNTIME_SERVER_BIN": str(Path(gsc_dir) / "bin" / "gsc-spec-server"),
            },
        }
    }


def gsc_plugins(gsc_dir: Path | None) -> list[dict[str, str]]:
    """Local Claude Code plugin attachment for GSC (SDK plugins → --plugin-dir)."""
    if gsc_dir is None:
        return []
    root = Path(gsc_dir)
    # GSC packaged runtime is a plugin root when .claude-plugin or plugin manifest exists.
    markers = (
        root / ".claude-plugin",
        root / "plugin.json",
        root / ".claude-plugin" / "plugin.json",
    )
    if any(m.exists() for m in markers) or (root / "mcp").is_dir():
        return [{"type": "local", "path": str(root)}]
    return []


def build_agent_options(
    *,
    cwd: Path | str,
    model: str,
    mcp_servers: dict[str, Any] | Path | None = None,
    plugins: list[dict[str, str]] | None = None,
    cli_path: Path | str | None = None,
    env: dict[str, str] | None = None,
    system_prompt_append: str | None = None,
    max_turns: int = 60,
    max_budget_usd: float | None = None,
    permission_mode: str = "acceptEdits",
) -> Any:
    """ClaudeAgentOptions mirroring the official starter, plus MCP/GSC."""
    ClaudeAgentOptions, *_ = _require_claude_sdk()
    options_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "allowed_tools": list(DEFAULT_ALLOWED_TOOLS),
        "disallowed_tools": list(DEFAULT_DISALLOWED_TOOLS),
        "permission_mode": permission_mode,
        "max_turns": max_turns,
        "strict_mcp_config": True,
        "extra_args": {
            "disable-slash-commands": None,
            "no-session-persistence": None,
            "autocompact": "200000",
        },
    }
    model = (model or "").strip()
    if model:
        options_kwargs["model"] = model
    if system_prompt_append:
        options_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt_append,
        }
    else:
        options_kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
    if mcp_servers is not None:
        options_kwargs["mcp_servers"] = mcp_servers
    if plugins:
        options_kwargs["plugins"] = plugins
    if cli_path is not None:
        options_kwargs["cli_path"] = str(cli_path)
    if env is not None:
        options_kwargs["env"] = dict(env)
    if max_budget_usd is not None:
        options_kwargs["max_budget_usd"] = float(max_budget_usd)
    return ClaudeAgentOptions(**options_kwargs)


def contest_system_prompt_append(skills_dir: Path | None) -> str:
    skills_txt = str(skills_dir) if skills_dir else ".claude/skills"
    return textwrap.dedent(
        f"""
        You implement an ARC-Bench application in the current working directory.
        The directory already contains an initialized starter application. Preserve existing work.

        Prefer GSC MCP tools (mcp__arch__*) for PRD/SPEC/state when available.
        ARC-Bench skills live under {skills_txt}. Force-load required skills each STEP
        (Skill tool when present; otherwise Read/Bash the skill's SKILL.md).
        Do not start a long-running server. Finish each STEP with a short summary.
        """
    ).strip()


_SKILL_MD_RE = re.compile(r"(?:\.claude/)?skills/([A-Za-z0-9._-]+)/SKILL\.md")


def _skill_name_from_skill_md_ref(text: str) -> str | None:
    if not text or "SKILL.md" not in text:
        return None
    m = _SKILL_MD_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or None


def _note_tool_use(
    name: str,
    inp: dict[str, Any] | None,
    *,
    skill_tool_available: bool,
    skill_loads: list[str],
    mcp_tool_counts: dict[str, int],
) -> None:
    if name.startswith("mcp__"):
        mcp_tool_counts[name] = mcp_tool_counts.get(name, 0) + 1
        print(
            json.dumps(
                {"event": "mcp_tool_use", "tool": name, "count": mcp_tool_counts[name]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    if name == "Skill":
        raw = ""
        if isinstance(inp, dict):
            raw = str(
                inp.get("skill")
                or inp.get("name")
                or inp.get("skill_name")
                or inp.get("skillName")
                or ""
            ).strip()
        if not raw:
            return
        skill_name = raw.rstrip("/").split("/")[-1]
        skill_loads.append(skill_name)
        print(
            json.dumps(
                {"event": "skill_loaded", "skill": skill_name, "via": "skill_tool"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    if name in ("Read", "Bash") and not skill_tool_available and isinstance(inp, dict):
        blob_parts: list[str] = []
        for key in ("file_path", "path", "filePath", "command", "cmd"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                blob_parts.append(val)
        skill_name = _skill_name_from_skill_md_ref("\n".join(blob_parts))
        if not skill_name:
            return
        skill_loads.append(skill_name)
        print(
            json.dumps(
                {
                    "event": "skill_loaded",
                    "skill": skill_name,
                    "via": "skill_md_read",
                    "tool": name,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


async def run_sdk_turn_async(
    *,
    prompt: str,
    options: Any,
    apply_openai_env: bool = True,
) -> SdkTurnResult:
    """One ClaudeSDKClient query + receive_response cycle (official pattern)."""
    (
        _ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        AssistantMessage,
        SystemMessage,
    ) = _require_claude_sdk()
    try:
        from claude_agent_sdk import ToolUseBlock
    except ImportError:
        ToolUseBlock = None  # type: ignore[misc, assignment]

    skill_loads: list[str] = []
    mcp_tool_counts: dict[str, int] = {}
    skill_tool_available = True
    tail_parts: list[str] = []
    result_msg: Any = None

    cm = claude_env_from_openai_env() if apply_openai_env else contextlib.nullcontext()
    with cm:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, SystemMessage):
                    data = getattr(message, "data", None) or {}
                    if str(getattr(message, "subtype", "") or "") == "init" or (
                        isinstance(data, dict) and data.get("subtype") == "init"
                    ):
                        tools = []
                        skills_field = None
                        src = data if isinstance(data, dict) else {}
                        if isinstance(src.get("tools"), list):
                            tools = src["tools"]
                        if "skills" in src:
                            skills_field = src.get("skills")
                        skills_empty = isinstance(skills_field, list) and len(skills_field) == 0
                        if tools and ("Skill" not in tools or skills_empty):
                            if skill_tool_available:
                                skill_tool_available = False
                                print(
                                    json.dumps(
                                        {
                                            "event": "skill_tool_unavailable",
                                            "tools_sample": tools[:12],
                                            "skills": skills_field,
                                            "driver": "ClaudeSDKClient",
                                            "note": "Skill tool missing or skills:[]; accept SKILL.md Read/Bash",
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                    continue

                if isinstance(message, AssistantMessage):
                    content = getattr(message, "content", None) or []
                    for block in content:
                        name = getattr(block, "name", None)
                        if ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                            name = block.name
                            inp = block.input if isinstance(block.input, dict) else {}
                        elif isinstance(block, dict):
                            name = block.get("name")
                            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        else:
                            inp = getattr(block, "input", None)
                            if not isinstance(inp, dict):
                                inp = {}
                        if isinstance(name, str):
                            _note_tool_use(
                                name,
                                inp if isinstance(inp, dict) else {},
                                skill_tool_available=skill_tool_available,
                                skill_loads=skill_loads,
                                mcp_tool_counts=mcp_tool_counts,
                            )
                    continue

                if isinstance(message, ResultMessage):
                    result_msg = message
                    subtype = str(getattr(message, "subtype", "") or "")
                    is_error = bool(getattr(message, "is_error", False))
                    terminal_reason = str(getattr(message, "terminal_reason", "") or "")
                    api_error_status = getattr(message, "api_error_status", None)
                    try:
                        api_error_status = int(api_error_status) if api_error_status is not None else None
                    except (TypeError, ValueError):
                        api_error_status = None
                    err_bits = getattr(message, "errors", None) or []
                    if err_bits:
                        tail_parts.append("; ".join(str(e) for e in err_bits))
                    result_text = getattr(message, "result", None)
                    if result_text:
                        tail_parts.append(str(result_text)[-2000:])
                    # Official: non-success subtypes raise; we map to harness result instead.
                    ok_subtypes = {"", "success", "completed", "done"}
                    failed = is_error or (subtype.lower() not in ok_subtypes and subtype.lower() not in {"success"})
                    if subtype and subtype.lower() not in ok_subtypes:
                        failed = True
                        if not terminal_reason:
                            terminal_reason = f"subtype:{subtype}"
                    returncode = 1 if failed else 0
                    if mcp_tool_counts:
                        print(
                            json.dumps(
                                {
                                    "event": "mcp_tool_use_summary",
                                    "tools": dict(sorted(mcp_tool_counts.items())),
                                    "total": sum(mcp_tool_counts.values()),
                                    "driver": "ClaudeSDKClient",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    if skill_loads:
                        print(
                            json.dumps(
                                {
                                    "event": "skill_load_summary",
                                    "skills": list(dict.fromkeys(skill_loads)),
                                    "total": len(skill_loads),
                                    "driver": "ClaudeSDKClient",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    return SdkTurnResult(
                        returncode=returncode,
                        is_error=failed,
                        terminal_reason=terminal_reason or ("error" if failed else "completed"),
                        subtype=subtype,
                        api_error_status=api_error_status,
                        tail="\n".join(tail_parts)[-8000:],
                        skills_loaded=tuple(dict.fromkeys(skill_loads)),
                        mcp_tools_used=tuple(sorted(mcp_tool_counts.keys())),
                    )

    # No ResultMessage — treat as failure.
    return SdkTurnResult(
        returncode=1,
        is_error=True,
        terminal_reason="missing_result_message",
        subtype="",
        api_error_status=None,
        tail="\n".join(tail_parts)[-8000:],
        skills_loaded=tuple(dict.fromkeys(skill_loads)),
        mcp_tools_used=tuple(sorted(mcp_tool_counts.keys())),
    )


def run_sdk_turn(
    *,
    prompt: str,
    options: Any,
    apply_openai_env: bool = True,
) -> SdkTurnResult:
    """Sync wrapper for harness STEP loops."""
    import asyncio

    return asyncio.run(
        run_sdk_turn_async(
            prompt=prompt,
            options=options,
            apply_openai_env=apply_openai_env,
        )
    )


def describe_driver_policy(
    *,
    model: str,
    enable_mcp: bool,
    mcp_config: Path | None,
    gsc_dir: Path | None,
    plugins: list[dict[str, str]],
) -> dict[str, Any]:
    """Structured log payload for arc_runtime_policy / official_claude_env."""
    plugin_note = None
    if enable_mcp and not plugins:
        plugin_note = (
            "GSC CLI plugin not attached via SDK plugins (markers missing or gsc_dir unset); "
            "MCP remains ON via mcp_servers/mcp-config"
        )
    return {
        "driver": "ClaudeSDKClient",
        "official_pattern": "implement_modules_async",
        "permission_mode": "acceptEdits",
        "model_from_MODEL": model,
        "anthropic_proxy": False,
        "mcp_enabled": enable_mcp,
        "mcp_config": str(mcp_config) if mcp_config else None,
        "mcp_via": "mcp_servers" if enable_mcp else None,
        "gsc_plugins": plugins,
        "gsc_plugin_note": plugin_note,
        "no_production_proxy": True,
        "no_cli_subprocess_primary": True,
    }
