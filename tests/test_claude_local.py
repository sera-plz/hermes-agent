"""Tests for the ``claude_local`` provider.

Covers the subprocess runtime (``agent.claude_local_runtime``), the transport
(``agent.transports.claude_local``), and the provider-selection wiring that
routes a turn to the local ``claude`` CLI instead of the Anthropic HTTP API.

No real ``claude`` binary is required: a tiny mock executable emits genuine
``stream-json`` so the parser, streaming callbacks, usage mapping, and error
classification are all exercised without spending subscription tokens.
"""

import json
import stat
import textwrap

import pytest

from agent.claude_local_runtime import (
    ALLOWED_TOOLS_ENV,
    CLAUDE_BIN_ENV,
    SKIP_PERMISSIONS_ENV,
    TIMEOUT_ENV,
    ClaudeAuthRequiredError,
    ClaudeLocalError,
    ClaudeNotInstalledError,
    ClaudeTimeoutError,
    build_claude_args,
    build_prompt_from_messages,
    claude_local_available,
    detect_login_required,
    run_claude_local,
)


# ── Mock-binary helpers ─────────────────────────────────────────────────────


def _write_mock(tmp_path, name: str, body: str) -> str:
    """Write an executable python mock and return its absolute path."""
    p = tmp_path / name
    p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IRWXU)
    return str(p)


def _make_emitter(
    tmp_path,
    name: str,
    events: list,
    *,
    exit_code: int = 0,
    stderr: str = "",
    sleep: float = 0.0,
) -> str:
    """Build a mock that drains stdin, optionally sleeps, then emits stream-json."""
    body = f"""
        import sys, json, time
        sys.stdin.read()
        time.sleep({sleep!r})
        for ev in {events!r}:
            sys.stdout.write(json.dumps(ev) + "\\n")
            sys.stdout.flush()
        if {stderr!r}:
            sys.stderr.write({stderr!r})
            sys.stderr.flush()
        sys.exit({exit_code!r})
    """
    return _write_mock(tmp_path, name, body)


_HAPPY_EVENTS = [
    {"type": "system", "subtype": "init", "model": "claude-opus-4-8", "session_id": "s1"},
    {
        "type": "assistant",
        "session_id": "s1",
        "message": {"content": [{"type": "text", "text": "hello world"}]},
    },
    {
        "type": "result",
        "session_id": "s1",
        "result": "hello world",
        "model": "claude-opus-4-8",
        "usage": {"input_tokens": 12, "cache_read_input_tokens": 4, "output_tokens": 9},
    },
]


# ── build_prompt_from_messages ──────────────────────────────────────────────


class TestBuildPrompt:
    def test_single_user_message_is_verbatim(self):
        system, prompt = build_prompt_from_messages(
            [{"role": "user", "content": "What is 2+2?"}]
        )
        assert system == ""
        assert prompt == "What is 2+2?"  # no "User:" prefix for the lone turn

    def test_multi_turn_transcript(self):
        system, prompt = build_prompt_from_messages(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "bye"},
            ]
        )
        assert system == ""
        assert prompt == "User: hi\n\nAssistant: hello\n\nUser: bye"

    def test_system_returned_separately(self):
        system, prompt = build_prompt_from_messages(
            [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "hi"},
            ]
        )
        assert system == "You are terse."
        assert prompt == "hi"

    def test_multiple_system_messages_concatenated(self):
        system, _ = build_prompt_from_messages(
            [
                {"role": "system", "content": "Rule A."},
                {"role": "system", "content": "Rule B."},
                {"role": "user", "content": "go"},
            ]
        )
        assert system == "Rule A.\n\nRule B."

    def test_list_vision_content_flattened_to_text(self):
        system, prompt = build_prompt_from_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                        {"type": "text", "text": "please"},
                    ],
                }
            ]
        )
        assert system == ""
        # image block dropped; text blocks flattened
        assert prompt == "describe this\nplease"


# ── build_claude_args ───────────────────────────────────────────────────────


class TestBuildArgs:
    def test_core_flags_and_model(self, monkeypatch):
        monkeypatch.delenv(SKIP_PERMISSIONS_ENV, raising=False)
        args = build_claude_args(model="claude-opus-4-8", system_text="be brief")
        assert args[:5] == ["--print", "-", "--output-format", "stream-json", "--verbose"]
        assert "--model" in args and args[args.index("--model") + 1] == "claude-opus-4-8"
        assert "--append-system-prompt" in args
        assert args[args.index("--append-system-prompt") + 1] == "be brief"
        # skip-permissions defaults ON so native tools run headlessly
        assert "--dangerously-skip-permissions" in args

    def test_effort_and_max_turns(self, monkeypatch):
        monkeypatch.delenv(SKIP_PERMISSIONS_ENV, raising=False)
        args = build_claude_args(
            model="m", system_text="", effort="high", max_turns=3
        )
        assert args[args.index("--effort") + 1] == "high"
        assert args[args.index("--max-turns") + 1] == "3"
        assert "--append-system-prompt" not in args  # empty system omitted

    def test_skip_permissions_off_uses_allowed_tools(self, monkeypatch):
        monkeypatch.setenv(SKIP_PERMISSIONS_ENV, "0")
        monkeypatch.setenv(ALLOWED_TOOLS_ENV, "Read,Grep")
        args = build_claude_args(model="m", system_text="")
        assert "--dangerously-skip-permissions" not in args
        assert args[args.index("--allowedTools") + 1] == "Read,Grep"

    def test_skip_permissions_off_without_allowlist(self, monkeypatch):
        monkeypatch.setenv(SKIP_PERMISSIONS_ENV, "0")
        monkeypatch.delenv(ALLOWED_TOOLS_ENV, raising=False)
        args = build_claude_args(model="m", system_text="")
        assert "--dangerously-skip-permissions" not in args
        assert "--allowedTools" not in args


# ── run_claude_local (mock binary) ──────────────────────────────────────────


class TestRunClaudeLocal:
    def test_happy_path(self, tmp_path, monkeypatch):
        mock = _make_emitter(tmp_path, "happy", _HAPPY_EVENTS)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        monkeypatch.setenv(SKIP_PERMISSIONS_ENV, "1")

        deltas = []
        first = {"fired": False}

        def _first():
            first["fired"] = True

        resp = run_claude_local(
            {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]},
            on_text_delta=deltas.append,
            on_first_delta=_first,
        )
        choice = resp.choices[0]
        assert choice.message.content == "hello world"
        assert choice.finish_reason == "stop"
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 9
        assert resp.usage.prompt_tokens_details.cached_tokens == 4
        assert resp.model == "claude-opus-4-8"
        assert deltas == ["hello world"]
        assert first["fired"] is True

    def test_error_max_turns_maps_to_length(self, tmp_path, monkeypatch):
        events = [
            {"type": "system", "subtype": "init", "model": "m"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
            {
                "type": "result",
                "subtype": "error_max_turns",
                "result": "partial",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ]
        mock = _make_emitter(tmp_path, "maxturns", events)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        resp = run_claude_local(
            {"model": "m", "messages": [{"role": "user", "content": "go"}]}
        )
        assert resp.choices[0].finish_reason == "length"
        assert resp.choices[0].message.content == "partial"

    def test_auth_required(self, tmp_path, monkeypatch):
        mock = _make_emitter(
            tmp_path,
            "auth",
            [],  # no result event
            exit_code=1,
            stderr="Invalid API key · Please run `claude login` to authenticate.",
        )
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        with pytest.raises(ClaudeAuthRequiredError):
            run_claude_local(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]}
            )

    def test_missing_binary(self, monkeypatch):
        monkeypatch.setenv(CLAUDE_BIN_ENV, "definitely-not-a-real-binary-xyz123")
        with pytest.raises(ClaudeNotInstalledError):
            run_claude_local(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]}
            )

    def test_nonzero_exit_without_result(self, tmp_path, monkeypatch):
        mock = _make_emitter(
            tmp_path, "boom", [], exit_code=2, stderr="some unexpected crash"
        )
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        with pytest.raises(ClaudeLocalError):
            run_claude_local(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]}
            )

    def test_timeout(self, tmp_path, monkeypatch):
        mock = _make_emitter(tmp_path, "slow", _HAPPY_EVENTS, sleep=10.0)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        monkeypatch.setenv(TIMEOUT_ENV, "1")
        with pytest.raises(ClaudeTimeoutError):
            run_claude_local(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]}
            )

    def test_interrupt_mid_stream(self, tmp_path, monkeypatch):
        mock = _make_emitter(tmp_path, "interrupt", _HAPPY_EVENTS, sleep=10.0)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        with pytest.raises(InterruptedError):
            run_claude_local(
                {"model": "m", "messages": [{"role": "user", "content": "go"}]},
                interrupt_check=lambda: True,
            )

    def test_long_prompt_no_stdin_deadlock(self, tmp_path, monkeypatch):
        mock = _make_emitter(tmp_path, "long", _HAPPY_EVENTS)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        big = "x" * 500_000
        resp = run_claude_local(
            {"model": "m", "messages": [{"role": "user", "content": big}]}
        )
        assert resp.choices[0].message.content == "hello world"


# ── detect_login_required ───────────────────────────────────────────────────


class TestDetectLogin:
    @pytest.mark.parametrize(
        "text",
        [
            "Please run `claude login`",
            "You are not logged in.",
            "invalid api key",
            "authentication_error",
            "OAuth token expired",
        ],
    )
    def test_positive(self, text):
        assert detect_login_required(text) is True

    @pytest.mark.parametrize("text", ["", "all good", "rate limit exceeded"])
    def test_negative(self, text):
        assert detect_login_required(text) is False


# ── claude_local_available ──────────────────────────────────────────────────


class TestAvailability:
    def test_true_when_binary_present(self, tmp_path, monkeypatch):
        mock = _make_emitter(tmp_path, "present", _HAPPY_EVENTS)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        assert claude_local_available() is True

    def test_false_when_missing(self, monkeypatch):
        monkeypatch.setenv(CLAUDE_BIN_ENV, "definitely-not-a-real-binary-xyz123")
        assert claude_local_available() is False


# ── Transport ───────────────────────────────────────────────────────────────


class TestTransport:
    def test_registered_for_api_mode(self):
        from agent.transports import get_transport

        t = get_transport("claude_local")
        assert t.api_mode == "claude_local"

    def test_build_kwargs_packs_options(self):
        from agent.transports import get_transport

        t = get_transport("claude_local")
        kwargs = t.build_kwargs(
            "claude-opus-4-8",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
        )
        assert kwargs["model"] == "claude-opus-4-8"
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert "__claude_local__" in kwargs
        assert "cwd" in kwargs["__claude_local__"]

    def test_normalize_response_from_mock_run(self, tmp_path, monkeypatch):
        from agent.transports import get_transport

        mock = _make_emitter(tmp_path, "norm", _HAPPY_EVENTS)
        monkeypatch.setenv(CLAUDE_BIN_ENV, mock)
        resp = run_claude_local(
            {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
        )
        normalized = get_transport("claude_local").normalize_response(resp)
        assert normalized.content == "hello world"
        assert normalized.finish_reason == "stop"
        assert normalized.usage is not None


# ── Provider-selection wiring ───────────────────────────────────────────────


class TestSelectionWiring:
    def test_determine_api_mode(self):
        from hermes_cli.providers import determine_api_mode

        for name in ("claude-local", "claude_local", "claude-cli", "claude-sub"):
            assert determine_api_mode(name) == "claude_local", name

    def test_normalize_provider_aliases(self):
        from hermes_cli.providers import normalize_provider

        for alias in ("claude_local", "claude-cli", "claude-sub", "claude-code-local"):
            assert normalize_provider(alias) == "claude-local", alias

    def test_get_provider_resolves(self):
        from hermes_cli.providers import get_provider

        pdef = get_provider("claude-local")
        assert pdef is not None
        assert pdef.transport == "claude_local"
        assert pdef.api_key_env_vars == ()

    def test_provider_profile_registered(self):
        from providers import get_provider_profile, list_providers

        prof = get_provider_profile("claude-local")
        assert prof is not None
        assert prof.api_mode == "claude_local"
        assert prof.env_vars == ()
        assert "claude-local" in {p.name for p in list_providers()}

    def test_resolve_runtime_provider(self):
        from hermes_cli.runtime_provider import resolve_runtime_provider

        rt = resolve_runtime_provider(requested="claude-local")
        assert rt["api_mode"] == "claude_local"
        assert rt["provider"] == "claude-local"

    def test_catalog_routes_to_accounts_tab(self):
        from hermes_cli.provider_catalog import provider_catalog_by_slug

        d = provider_catalog_by_slug().get("claude-local")
        assert d is not None
        # No API-key entry: subprocess owns its own credentials.
        assert d.tab == "accounts"
        assert d.api_key_env_vars == ()

    def test_appears_in_picker_universe(self):
        from hermes_cli.models import CANONICAL_PROVIDERS

        assert "claude-local" in {p.slug for p in CANONICAL_PROVIDERS}
