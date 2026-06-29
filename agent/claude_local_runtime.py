"""Claude CLI subprocess runtime for the ``claude_local`` api_mode.

This module is the execution layer for the ``claude-local`` provider: instead
of making an HTTP call to the Anthropic API, it spawns the local ``claude``
CLI (the Claude Code binary) as a subprocess and drives it through stdin/stdout
using the CLI's ``--output-format stream-json`` protocol.

The technique is ported from Paperclip's ``claude-local`` adapter
(``packages/adapters/claude-local/src/server/{execute,parse}.ts``).  The two
big differences from Paperclip:

* **Stateless per turn.** Hermes already owns the full conversation history, so
  every turn serialises the whole transcript into a single ``--print`` prompt
  rather than using ``--resume`` session continuity.
* **Native tools, no round-trip.** Hermes does not translate ``tool_use`` /
  ``tool_result`` blocks.  The subprocess runs with its *own* native toolset
  enabled (Read/Edit/Bash/…) in the agent's working directory, so Claude uses
  tools directly and only the final assistant text is returned to Hermes.

Auth comes entirely from Claude's own credential store (``~/.claude/`` or
``$CLAUDE_CONFIG_DIR``) — **no ``ANTHROPIC_API_KEY`` is required**, so usage
bills against the Claude Code subscription rather than API credits.

The runtime returns an OpenAI ``ChatCompletion``-shaped object (a
``SimpleNamespace`` tree) so that the rest of the agent loop — and the
``ChatCompletionsTransport.normalize_response`` we inherit — can consume it
with zero special-casing, exactly like ``bedrock_adapter.normalize_converse_response``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default binary; overridable via env for non-PATH installs.
CLAUDE_BIN_ENV = "HERMES_CLAUDE_LOCAL_BIN"
# Per-turn wall-clock budget (seconds). 0 / unset → no Hermes-side timeout.
TIMEOUT_ENV = "HERMES_CLAUDE_LOCAL_TIMEOUT"
# Grace period after a terminal ``result`` event before force-killing.
GRACE_ENV = "HERMES_CLAUDE_LOCAL_GRACE"
# Permission handling. Default ON so native write/bash tools work headless
# (``--print`` mode cannot answer interactive permission prompts). Set to
# "0"/"false"/"no" to drop the flag and restrict to no-permission tools.
SKIP_PERMISSIONS_ENV = "HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS"
# Optional comma-separated allowlist passed as --allowedTools (used instead of
# --dangerously-skip-permissions when skip-permissions is disabled).
ALLOWED_TOOLS_ENV = "HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS"


class ClaudeLocalError(RuntimeError):
    """Base error for claude_local execution failures."""


class ClaudeNotInstalledError(ClaudeLocalError):
    """The ``claude`` CLI could not be found on PATH."""


class ClaudeAuthRequiredError(ClaudeLocalError):
    """Claude is not logged in / its credentials have expired."""


class ClaudeTimeoutError(ClaudeLocalError):
    """The subprocess exceeded the configured wall-clock budget."""


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _claude_binary() -> str:
    return (os.getenv(CLAUDE_BIN_ENV) or "claude").strip() or "claude"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def claude_local_available() -> bool:
    """Return True when the ``claude`` CLI is resolvable on PATH."""
    return shutil.which(_claude_binary()) is not None


# ---------------------------------------------------------------------------
# Message → prompt conversion
# ---------------------------------------------------------------------------


def _content_to_text(content: Any) -> str:
    """Flatten OpenAI message ``content`` (str or content-block list) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # OpenAI vision/content-block shapes: {"type":"text","text":...}
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
                # image / other blocks are dropped — claude_local is text-only v1
        return "\n".join(p for p in parts if p)
    return str(content)


def build_prompt_from_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Split OpenAI-format messages into (system_text, transcript_prompt).

    System messages are concatenated and returned separately so the caller can
    pass them via ``--append-system-prompt``.  Every other turn is serialised
    into a single human-readable transcript that becomes the ``--print`` stdin
    prompt (stateless: the full history is replayed each turn).
    """
    system_parts: list[str] = []
    transcript: list[str] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        text = _content_to_text(msg.get("content"))

        if role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if role == "assistant":
            # Surface any tool calls the assistant previously requested as text
            # context (we do not replay them as structured tool_use).
            tool_calls = msg.get("tool_calls")
            if tool_calls and not text.strip():
                try:
                    names = ", ".join(
                        tc.get("function", {}).get("name", "tool")
                        for tc in tool_calls
                        if isinstance(tc, dict)
                    )
                    text = f"[called tools: {names}]"
                except Exception:
                    pass
            if text.strip():
                transcript.append(f"Assistant: {text.strip()}")
            continue
        if role == "tool":
            if text.strip():
                transcript.append(f"Tool result: {text.strip()}")
            continue
        # user (and any unknown role)
        if text.strip():
            transcript.append(f"User: {text.strip()}")

    system_text = "\n\n".join(system_parts).strip()
    prompt = "\n\n".join(transcript).strip()
    # When the only turn is a single user message, send it verbatim (cleaner
    # than a "User: ..." prefixed transcript of length one).
    if len([t for t in transcript if t.startswith("User:")]) == 1 and len(transcript) == 1:
        prompt = transcript[0][len("User: "):]
    return system_text, prompt


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def build_claude_args(
    *,
    model: str | None,
    system_text: str,
    effort: str | None = None,
    max_turns: int | None = None,
) -> list[str]:
    """Construct the ``claude`` CLI argv (excluding the binary itself)."""
    args = ["--print", "-", "--output-format", "stream-json", "--verbose"]

    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", str(effort)]
    if max_turns and max_turns > 0:
        args += ["--max-turns", str(max_turns)]
    if system_text:
        args += ["--append-system-prompt", system_text]

    # Permission / tool access. Native tools need this to run headlessly.
    if _truthy(os.getenv(SKIP_PERMISSIONS_ENV), default=True):
        args.append("--dangerously-skip-permissions")
    else:
        allowed = (os.getenv(ALLOWED_TOOLS_ENV) or "").strip()
        if allowed:
            args += ["--allowedTools", allowed]

    return args


# ---------------------------------------------------------------------------
# stream-json parsing
# ---------------------------------------------------------------------------


_LOGIN_RE = re.compile(
    r"(?:not\s+logged\s+in|please\s+log\s+in|run\s+`?claude\s+login`?|"
    r"invalid\s+api\s+key|authentication[_\s]?error|oauth\s+token\s+(?:expired|revoked))",
    re.IGNORECASE,
)


def _parse_stream_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_text_blocks(message: dict) -> list[str]:
    out: list[str] = []
    content = message.get("content")
    if isinstance(content, list):
        for entry in content:
            if isinstance(entry, dict) and entry.get("type") == "text":
                t = entry.get("text")
                if isinstance(t, str) and t:
                    out.append(t)
    elif isinstance(content, str) and content:
        out.append(content)
    return out


def detect_login_required(text: str) -> bool:
    return bool(_LOGIN_RE.search(text or ""))


# ---------------------------------------------------------------------------
# OpenAI-shaped response construction
# ---------------------------------------------------------------------------


def _build_openai_response(
    *,
    content: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    """Return a ChatCompletion-shaped object the inherited transport can read."""
    message = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=None,
        refusal=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason, index=0)
    usage = SimpleNamespace(
        prompt_tokens=int(input_tokens or 0),
        completion_tokens=int(output_tokens or 0),
        total_tokens=int((input_tokens or 0) + (output_tokens or 0)),
        prompt_tokens_details=SimpleNamespace(cached_tokens=int(cached_tokens or 0)),
    )
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model=model,
        _claude_local=True,
    )


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------


def run_claude_local(
    api_kwargs: dict,
    *,
    on_text_delta: Callable[[str], None] | None = None,
    on_first_delta: Callable[[], None] | None = None,
    interrupt_check: Callable[[], bool] | None = None,
) -> SimpleNamespace:
    """Spawn the Claude CLI, stream its output, and return an OpenAI-shaped response.

    ``api_kwargs`` is produced by ``ClaudeLocalTransport.build_kwargs`` and
    carries ``model``, ``messages`` and a ``__claude_local__`` options dict.
    Streaming callbacks fire per assistant text delta when provided.
    """
    opts = dict(api_kwargs.get("__claude_local__") or {})
    model = api_kwargs.get("model") or opts.get("model") or ""
    messages = api_kwargs.get("messages") or []

    system_text, prompt = build_prompt_from_messages(messages)
    if not prompt and system_text:
        # Degenerate case: only a system message. Use it as the prompt.
        prompt, system_text = system_text, ""

    binary = _claude_binary()
    if shutil.which(binary) is None:
        raise ClaudeNotInstalledError(
            f"The '{binary}' CLI was not found on PATH. Install Claude Code "
            f"(https://docs.claude.com/claude-code) or set {CLAUDE_BIN_ENV}."
        )

    args = build_claude_args(
        model=model or None,
        system_text=system_text,
        effort=opts.get("effort"),
        max_turns=opts.get("max_turns"),
    )
    cwd = opts.get("cwd") or os.getcwd()
    timeout = _env_float(TIMEOUT_ENV, float(opts.get("timeout") or 0))
    grace = _env_float(GRACE_ENV, 20.0)

    env = dict(os.environ)
    # Ensure the child reaches Claude's own credential store. Never inject an
    # ANTHROPIC_API_KEY — auth must come from the subscription OAuth creds.
    cfg_dir = opts.get("claude_config_dir") or env.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg_dir

    logger.debug(
        "claude_local: spawning %s %s (cwd=%s, timeout=%s)",
        binary, " ".join(args[:6]) + " …", cwd, timeout or "none",
    )

    try:
        proc = subprocess.Popen(
            [binary, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ClaudeNotInstalledError(
            f"Failed to launch '{binary}': {exc}"
        ) from exc

    # Write the prompt to stdin in a separate thread so a large prompt can't
    # deadlock against a full stdout pipe.
    def _feed_stdin():
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except Exception:
            pass

    threading.Thread(target=_feed_stdin, daemon=True).start()

    assistant_texts: list[str] = []
    seen_models: list[str] = []
    final_result: dict | None = None
    first_delta_fired = {"done": False}
    stdout_lines: list[str] = []

    def _fire_first():
        if not first_delta_fired["done"] and on_first_delta:
            first_delta_fired["done"] = True
            try:
                on_first_delta()
            except Exception:
                pass

    timed_out = {"value": False}

    # Wall-clock watchdog (optional) + interrupt watchdog.
    stop_watch = threading.Event()

    def _watchdog():
        waited = 0.0
        step = 0.25
        while not stop_watch.wait(step):
            waited += step
            if interrupt_check and interrupt_check():
                _kill(proc)
                return
            if timeout and waited >= timeout:
                timed_out["value"] = True
                _kill(proc)
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        if proc.stdout:
            for raw in proc.stdout:
                stdout_lines.append(raw)
                event = _parse_stream_line(raw)
                if event is None:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    m = event.get("model")
                    if isinstance(m, str) and m:
                        seen_models.append(m)
                elif etype == "assistant":
                    msg = event.get("message")
                    if isinstance(msg, dict):
                        for t in _extract_text_blocks(msg):
                            assistant_texts.append(t)
                            _fire_first()
                            if on_text_delta:
                                try:
                                    on_text_delta(t)
                                except Exception:
                                    pass
                elif etype == "result":
                    final_result = event
                    m = event.get("model")
                    if isinstance(m, str) and m:
                        seen_models.append(m)
                    # Terminal event seen — stop reading promptly.
                    break
    finally:
        stop_watch.set()

    try:
        proc.wait(timeout=grace if grace > 0 else None)
    except subprocess.TimeoutExpired:
        _kill(proc)

    stderr_text = ""
    try:
        if proc.stderr:
            stderr_text = proc.stderr.read() or ""
    except Exception:
        pass

    stdout_text = "".join(stdout_lines)

    if timed_out["value"]:
        raise ClaudeTimeoutError(f"claude_local timed out after {timeout:.0f}s")

    if interrupt_check and interrupt_check():
        raise InterruptedError("Agent interrupted during claude_local call")

    # ── Error classification ────────────────────────────────────────────
    exit_code = proc.returncode or 0
    combined = f"{stdout_text}\n{stderr_text}"

    if final_result is None:
        # No terminal result event — inspect for known failure signatures.
        if detect_login_required(combined):
            raise ClaudeAuthRequiredError(
                "Claude is not authenticated. Run `claude login` (or `claude "
                "setup-token`) to sign in with your Claude subscription."
            )
        if exit_code != 0:
            snippet = (stderr_text or stdout_text or "").strip()[:500]
            raise ClaudeLocalError(
                f"claude_local subprocess exited {exit_code} without a result. "
                f"{snippet}"
            )
        # Exit 0 but no result event — fall back to any streamed assistant text.
        if not assistant_texts:
            raise ClaudeLocalError(
                "claude_local produced no result event and no assistant output."
            )

    # ── Build the response ──────────────────────────────────────────────
    result = final_result or {}
    summary = result.get("result")
    if not isinstance(summary, str) or not summary.strip():
        summary = "\n\n".join(assistant_texts).strip()

    # A result event can itself signal an auth/transient error.
    subtype = str(result.get("subtype") or "")
    is_error = bool(result.get("is_error"))
    if is_error and detect_login_required(f"{summary}\n{combined}"):
        raise ClaudeAuthRequiredError(
            "Claude is not authenticated. Run `claude login` to sign in with "
            "your Claude subscription."
        )

    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    finish_reason = "length" if subtype == "error_max_turns" else "stop"
    model_out = (seen_models[-1] if seen_models else "") or model or "claude-local"

    return _build_openai_response(
        content=summary,
        model=model_out,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cached_tokens=usage.get("cache_read_input_tokens", 0),
        finish_reason=finish_reason,
    )


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
