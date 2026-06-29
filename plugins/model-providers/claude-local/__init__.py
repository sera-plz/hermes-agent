"""Claude Local provider profile — routes through the Claude CLI subprocess.

Selecting this provider runs each turn through the local ``claude`` binary
(Claude Code) instead of the Anthropic HTTP API. Authentication comes from
Claude's own credential store (``~/.claude/`` or ``$CLAUDE_CONFIG_DIR``), so
**no ``ANTHROPIC_API_KEY`` is required** and usage bills against the Claude
Code subscription rather than API credits.

    hermes model --provider claude-local

The subprocess runs with its native toolset enabled in the working directory,
so Claude can read/edit files and run commands directly (see
``agent/claude_local_runtime.py`` for the env-var knobs).
"""

from providers import register_provider
from providers.base import ProviderProfile

# Importing the transport module triggers its self-registration for the
# ``claude_local`` api_mode, so the provider works even if the bundled
# transport discovery list hasn't been updated.
try:  # pragma: no cover - defensive
    import agent.transports.claude_local  # noqa: F401
except Exception:
    pass

claude_local = ProviderProfile(
    name="claude-local",
    aliases=("claude_local", "claude-cli", "claude-sub", "claude-code-local"),
    display_name="Claude Local (CLI subprocess)",
    description="Claude Code via local `claude` CLI — uses your subscription, no API key",
    signup_url="https://docs.claude.com/claude-code",
    api_mode="claude_local",
    env_vars=(),  # subprocess auth via ~/.claude — no API key
    base_url="",
    auth_type="claude_cli",
    # Reasonable defaults so the model picker has something to show. The CLI's
    # --model accepts these aliases; fall back to the account default if unset.
    fallback_models=(
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ),
    default_aux_model="claude-haiku-4-5-20251001",
)

register_provider(claude_local)
