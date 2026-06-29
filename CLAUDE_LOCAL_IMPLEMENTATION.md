# CLAUDE_LOCAL_IMPLEMENTATION.md — Phase 2

Implements the `claude_local` provider: a new `api_mode="claude_local"` that
spawns the local `claude` CLI as a subprocess instead of calling the Anthropic
HTTP API. Auth comes from Claude's own credential store, so usage bills against
the **Claude Code subscription** — no `ANTHROPIC_API_KEY` required.

**Path chosen:** HYBRID (per Phase 1 decision) — pluggable provider profile +
auto-registered transport + a small subprocess runtime, plus **3 surgical
additive `elif` branches** in the execution dispatcher.

---

## Files

### New files (the pluggable bulk)

| File | Role |
|---|---|
| `agent/claude_local_runtime.py` | Subprocess runner. Builds the `claude` argv, spawns it, parses `stream-json` stdout, classifies errors, returns an **OpenAI `ChatCompletion`-shaped** object. Python port of Paperclip's `execute.ts` + `parse.ts`. |
| `agent/transports/claude_local.py` | `ClaudeLocalTransport(ChatCompletionsTransport)`. Overrides `api_mode` + `build_kwargs`; **inherits** `normalize_response` / `validate_response` because the runtime returns an OpenAI-shaped response. Self-registers for `"claude_local"`. |
| `plugins/model-providers/claude-local/__init__.py` | `ProviderProfile(name="claude-local", api_mode="claude_local", auth_type="claude_cli", env_vars=())`, aliases `claude_local`, `claude-cli`, `claude-sub`, `claude-code-local`. |
| `plugins/model-providers/claude-local/plugin.yaml` | Plugin manifest. |

### Core edits (the minimal additive fork)

| File | Change |
|---|---|
| `agent/transports/__init__.py` | `_discover_transports()`: added guarded `import agent.transports.claude_local`. |
| `agent/chat_completion_helpers.py` `build_api_kwargs()` | Added `if agent.api_mode == "claude_local":` branch → `transport.build_kwargs(...)`. |
| `agent/chat_completion_helpers.py` `interruptible_api_call()` | Added `elif agent.api_mode == "claude_local":` → `run_claude_local(api_kwargs, interrupt_check=…)`. |
| `agent/chat_completion_helpers.py` `interruptible_streaming_api_call()` | Added `if agent.api_mode == "claude_local":` worker → `run_claude_local(..., on_text_delta=…, on_first_delta=…)`. |

No edits were needed in `conversation_loop.py`: `claude_local` falls through the
existing `else` (chat_completions) branches for validate / finish_reason /
normalize, and since the runtime returns an OpenAI-shaped object and the
transport subclasses `ChatCompletionsTransport`, those branches Just Work.

---

## How it works (one turn, end to end)

```
conversation_loop._build_api_kwargs()
  └─ build_api_kwargs(): api_mode=="claude_local"
       └─ ClaudeLocalTransport.build_kwargs(model, messages, …)
            → {"model", "messages" (OpenAI fmt), "tools", "__claude_local__": {...}}
  ↓
_perform_api_call → interruptible[_streaming]_api_call(): api_mode=="claude_local"
  └─ run_claude_local(api_kwargs):
       1. build_prompt_from_messages() → (system_text, transcript_prompt)
       2. build_claude_args() → ["--print","-","--output-format","stream-json","--verbose",
                                 "--model", M, "--effort", E, "--append-system-prompt", SYS,
                                 "--dangerously-skip-permissions"]
       3. subprocess.Popen([claude, *args]); prompt → stdin (fed on a thread)
       4. read stdout lines: system/init → model; assistant → text deltas (+ callbacks);
          result → terminal usage/summary
       5. classify errors (not-installed / auth / timeout / max-turns)
       6. return OpenAI-shaped SimpleNamespace(choices=[…], usage=…, model=…)
  ↓
conversation_loop else-branch: transport.normalize_response(resp) → NormalizedResponse
```

### Stateless, native-tools design (per Phase 1 decisions)
- **Stateless `--print` per turn.** Hermes owns history; the full transcript is
  serialised into one prompt each turn (no `--resume`).
- **Native tools, no round-trip.** Hermes tool schemas are *not* forwarded.
  The subprocess uses its own native tools (Read/Edit/Bash/…) directly in the
  agent's working dir. To make that work headlessly, `--dangerously-skip-permissions`
  is passed by default (overridable — see env vars).

---

## Configuration (env vars)

All optional; sensible defaults.

| Env var | Default | Meaning |
|---|---|---|
| `HERMES_CLAUDE_LOCAL_BIN` | `claude` | Path/name of the CLI binary. |
| `HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS` | `1` (on) | Pass `--dangerously-skip-permissions` so native write/bash tools run headlessly. Set `0` to restrict to no-permission tools. |
| `HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS` | — | Comma-separated `--allowedTools` list, used when skip-permissions is `0`. |
| `HERMES_CLAUDE_LOCAL_MAX_TURNS` | — | `--max-turns` cap for the subprocess agent loop. |
| `HERMES_CLAUDE_LOCAL_TIMEOUT` | `0` (none) | Per-turn wall-clock budget (seconds). |
| `HERMES_CLAUDE_LOCAL_GRACE` | `20` | Grace seconds after a terminal `result` before force-kill. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Honored (passed through to the child) — standard Claude credential dir. |

---

## Verification performed (pre-Phase-4)

Run with `uv run python` (project env). Used a **mock `claude` binary** emitting
real `stream-json` so no subscription tokens were spent.

- ✅ Transport resolves: `get_transport('claude_local')` → `ClaudeLocalTransport` (subclass of `ChatCompletionsTransport`).
- ✅ Profile registered: `get_provider_profile('claude-local')` and alias `claude_local`; appears in `list_providers()` (33 providers total).
- ✅ Prompt conversion: single-user → verbatim; multi-turn → `User:/Assistant:` transcript; system → `--append-system-prompt`.
- ✅ argv construction correct (model/effort/max-turns/skip-permissions).
- ✅ End-to-end mock run: streamed deltas fire (`on_first_delta` + per-text), content assembled, usage mapped (`input_tokens→prompt_tokens`, `cache_read_input_tokens→cached`), `normalize_response` produces correct `NormalizedResponse`.
- ✅ Error paths: not-installed → `ClaudeNotInstalledError`; auth → `ClaudeAuthRequiredError` ("run `claude login`"); `error_max_turns` → `finish_reason="length"`.
- ✅ All edited core modules import and compile with no regressions.

Full logs in `CLAUDE_LOCAL_TEST_RESULTS.md` (Phase 4).

---

## Known limitations / follow-ups
- **Text-only.** No structured tool-call round-tripping into Hermes (by design — the subprocess executes tools itself). Image/content blocks in messages are dropped from the prompt.
- **Model catalog.** `claude-local` has no live `/v1/models` endpoint; the picker shows `fallback_models`. Bare-name auto-detect (`detect_provider_for_model('claude-local')`) returns `None` because there's no static catalog — selection is via the interactive picker / config (Phase 3).
- **Cost.** `total_cost_usd` from the CLI is parsed but not yet surfaced into Hermes' usage accounting (subscription usage isn't credit-based anyway).

**Phase 2 complete. Awaiting checkpoint review before Phase 3 (integration & config).**
