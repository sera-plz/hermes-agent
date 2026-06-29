"""Claude CLI (subprocess) transport — api_mode='claude_local'.

Routes a turn through the local ``claude`` binary instead of the Anthropic
HTTP API (see :mod:`agent.claude_local_runtime`).  Auth comes from Claude's
own credential store, so usage bills against the Claude Code subscription
rather than API credits.

The transport itself only handles *format conversion*: ``build_kwargs`` packs
the OpenAI-format messages plus a ``__claude_local__`` options dict that the
execution dispatch in ``chat_completion_helpers`` hands to
``claude_local_runtime.run_claude_local``.  Because that runtime returns an
OpenAI ``ChatCompletion``-shaped object, we subclass ``ChatCompletionsTransport``
and inherit ``normalize_response`` / ``validate_response`` / ``preflight``
unchanged — no bespoke normalization is needed.
"""

from __future__ import annotations

import os
from typing import Any

from agent.transports.chat_completions import ChatCompletionsTransport


class ClaudeLocalTransport(ChatCompletionsTransport):
    """Transport for api_mode='claude_local' (Claude CLI subprocess)."""

    @property
    def api_mode(self) -> str:
        return "claude_local"

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Hermes tool schemas are NOT forwarded to the CLI — the subprocess
        # uses its own native toolset directly. Returning them unchanged keeps
        # the base contract happy; the runtime simply ignores them.
        return tools or []

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Pack messages + claude_local execution options.

        The returned dict is consumed by ``run_claude_local`` (not an HTTP
        client), so it deliberately carries the raw OpenAI-format ``messages``
        plus a ``__claude_local__`` options bag rather than wire-shaped kwargs.
        """
        reasoning = params.get("reasoning_config") or {}
        effort = None
        if isinstance(reasoning, dict) and reasoning.get("enabled") is not False:
            _e = str(reasoning.get("effort", "") or "").strip().lower()
            if _e in {"low", "medium", "high", "xhigh"}:
                effort = _e

        options: dict[str, Any] = {
            "effort": effort,
            "max_turns": params.get("claude_local_max_turns")
            or _env_int("HERMES_CLAUDE_LOCAL_MAX_TURNS"),
            "timeout": params.get("timeout"),
            "cwd": params.get("cwd") or os.getcwd(),
            "claude_config_dir": os.getenv("CLAUDE_CONFIG_DIR"),
        }

        return {
            "model": model,
            "messages": messages,
            "tools": tools or [],
            "__claude_local__": options,
        }


def _env_int(name: str) -> int | None:
    try:
        raw = os.getenv(name)
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Auto-register on import
from agent.transports import register_transport  # noqa: E402

register_transport("claude_local", ClaudeLocalTransport)
