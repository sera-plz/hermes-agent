# CLAUDE_LOCAL_HANDOFF.md — Remaining work (Phases 3–5)

> **For the cloud agent finishing this feature.** Phases 1–2 are **done and
> committed** (`bf91db9694 add claude local plugin`). This document is
> self-contained: it tells you exactly what is already wired, the integration
> gaps that remain, the precise files/functions to edit, and how to validate.
> Read `CLAUDE_LOCAL_PLAN.md` (architecture decision) and
> `CLAUDE_LOCAL_IMPLEMENTATION.md` (what was built) first — they're in the repo
> root.

## Goal (recap)
A `claude_local` provider that routes each Hermes turn through the local
`claude` CLI subprocess instead of the Anthropic HTTP API, so usage bills
against the **Claude Code subscription** (OAuth in `~/.claude/`) — **no
`ANTHROPIC_API_KEY`**.

## Environment / how to run
- Project uses **`uv`**. Always run Python as `uv run python …` and the CLI as
  `uv run hermes …` (system `python3` lacks deps like `yaml`).
- `claude` is on PATH on the dev machine; the cloud box **may not have it** —
  use the mock-binary technique (below) for any test that shouldn't spend
  subscription tokens, and gate live tests on `shutil.which("claude")`.

---

## ✅ Already done & committed (Phase 1–2)

New files:
- `agent/claude_local_runtime.py` — spawns `claude --print - --output-format stream-json --verbose`, feeds the prompt on stdin, parses stream-json, classifies errors, returns an **OpenAI `ChatCompletion`-shaped** object. Public API: `run_claude_local(api_kwargs, *, on_text_delta, on_first_delta, interrupt_check)`, `claude_local_available()`, `build_prompt_from_messages()`, `build_claude_args()`, exceptions `ClaudeNotInstalledError` / `ClaudeAuthRequiredError` / `ClaudeTimeoutError`.
- `agent/transports/claude_local.py` — `ClaudeLocalTransport(ChatCompletionsTransport)`; `api_mode="claude_local"`; inherits `normalize_response`/`validate_response`. Self-registers.
- `plugins/model-providers/claude-local/{__init__.py,plugin.yaml}` — `ProviderProfile(name="claude-local", api_mode="claude_local", auth_type="claude_cli", env_vars=())`, aliases `claude_local`, `claude-cli`, `claude-sub`, `claude-code-local`.

Core edits (all additive `elif api_mode == "claude_local"`):
- `agent/transports/__init__.py::_discover_transports()` — imports the new transport.
- `agent/chat_completion_helpers.py` — `build_api_kwargs()`, `interruptible_api_call()`, `interruptible_streaming_api_call()`.

Verified end-to-end with a mock `claude` binary: registration, prompt
conversion, stream-json parsing, streaming deltas, usage mapping, and the three
error paths. **No `conversation_loop.py` edits were needed** — `claude_local`
rides the existing `else`/chat_completions branches.

---

## ⚠️ The one integration gap that blocks selection (DO THIS FIRST)

There are **two separate provider registries**, and only one is wired:

| Registry | File | Purpose | Status |
|---|---|---|---|
| `ProviderProfile` | `providers/` (+ `plugins/model-providers/`) | Drives `build_kwargs` / profile path | ✅ claude-local registered |
| `ProviderDef` / overlays | `hermes_cli/providers.py` | Drives **`determine_api_mode()`** and the model picker | ❌ claude-local **missing** |

`hermes_cli/providers.py::determine_api_mode(provider, base_url)` is what maps a
selected provider → `api_mode`. Today, selecting `claude-local` would fall
through to the default and return `"chat_completions"` — so the subprocess path
would never fire. **Fix this or nothing else works.**

### Required edits in `hermes_cli/providers.py`

1. **Add a direct api_mode mapping** in `determine_api_mode()` (mirror the
   existing `bedrock` special-case, ~line 558):
   ```python
   if provider in ("claude-local", "claude_local"):
       return "claude_local"
   ```
   Place it alongside `if provider == "bedrock": return "bedrock_converse"`.

2. **Register the overlay** so the picker recognizes it and doesn't demand an
   API key. Add to `HERMES_OVERLAYS` (~line 46):
   ```python
   "claude-local": HermesOverlay(
       transport="claude_local",          # also add to TRANSPORT_TO_API_MODE below
       auth_type="external_process",      # signals "no API key needed" to the picker
       base_url_override="",
   ),
   ```
   (Check `HermesOverlay`'s real field names — `auth_type` values in use include
   `virtual`, `oauth_external`, `external_process`. Pick the one that makes the
   picker skip API-key entry, like `moa`/`openai-codex` do.)

3. **Extend `TRANSPORT_TO_API_MODE`** (~line 381) so the overlay's transport
   resolves even via the generic path:
   ```python
   "claude_local": "claude_local",
   ```

4. **Add aliases** so `normalize_provider()` canonicalizes user input. Find the
   `ALIASES` dict in this file and add:
   ```python
   "claude_local": "claude-local",
   "claude-cli": "claude-local",
   "claude-sub": "claude-local",
   ```

5. **Sanity check** `get_provider("claude-local")` returns a `ProviderDef` after
   this (it layers models.dev + overlay; a no-catalog provider should still
   resolve via the overlay — verify and patch `get_provider`/`resolve_provider_full`
   if a missing models.dev entry makes it return `None`).

### Acceptance for the gap
```bash
uv run python -c "
from hermes_cli.providers import determine_api_mode, normalize_provider, get_provider
assert determine_api_mode('claude-local') == 'claude_local'
assert normalize_provider('claude_local') == 'claude-local'
print('api_mode OK; get_provider:', get_provider('claude-local') is not None)
"
```

---

## Phase 3 — Integration & configuration

After the gap fix:

1. **Make it selectable.** `hermes model` is an **interactive picker** — there
   is **no `--provider` flag** (the original brief's `hermes model --provider
   claude_local` does not exist; don't document it as-is). Two real selection
   paths:
   - Interactive: `uv run hermes model` → choose "Claude Local (CLI subprocess)".
     Confirm it appears in the list (it's already in `providers.list_providers()`).
   - Config file: `~/.hermes/config.yaml`. Inspect how `model_switch.py`
     persists a selection (look at `hermes_cli/model_switch.py` around
     lines 1129–1314 and `hermes_cli/config.py` `DEFAULT_CONFIG` `"model"` /
     `"providers"` keys) and document the exact YAML a user writes to pin
     `provider: claude-local` + a `model:` (e.g. `claude-opus-4-8`) and
     `api_mode: claude_local` if the config supports an override.

2. **Verify the picker doesn't ask for an API key** (auth_type must signal
   subprocess auth). If it does, fix the overlay `auth_type`.

3. **Write `CLAUDE_LOCAL_CONFIG.md`** (or extend the README) with:
   - The exact `config.yaml` snippet to select claude-local.
   - The env-var table from `CLAUDE_LOCAL_IMPLEMENTATION.md` (binary path,
     skip-permissions, max-turns, timeout, `CLAUDE_CONFIG_DIR`).
   - The `claude login` prerequisite.

Deliverable: updated config + integration docs; `claude-local` selectable and
appears in `hermes model`.

---

## Phase 4 — Testing & validation

Deliverable: **`CLAUDE_LOCAL_TEST_RESULTS.md`** with commands, logs, and
pass/fail per item.

### A. Unit tests (no real `claude`; use a mock binary)
Add `tests/test_claude_local.py` (find the repo's test runner — likely
`uv run pytest`). Cover:
- `build_prompt_from_messages`: single-user verbatim; multi-turn transcript;
  system → returned separately; list/vision content flattened to text.
- `build_claude_args`: model/effort/max-turns/skip-permissions flag assembly;
  `HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS=0` → `--allowedTools` path.
- `run_claude_local` with a mock binary (pattern below): happy path → content +
  usage; `error_max_turns` → `finish_reason="length"`; auth stderr →
  `ClaudeAuthRequiredError`; missing binary → `ClaudeNotInstalledError`.
- `ClaudeLocalTransport.normalize_response(resp)` on a mock response →
  correct `NormalizedResponse`.

**Mock binary pattern** (proven in Phase 2):
```python
# fake_claude.py — emits real stream-json then exits 0
import sys, json
sys.stdin.read()
for ev in [
    {"type":"system","subtype":"init","model":"claude-opus-4-8"},
    {"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}},
    {"type":"result","result":"hello","model":"claude-opus-4-8",
     "usage":{"input_tokens":1,"cache_read_input_tokens":0,"output_tokens":1}},
]:
    print(json.dumps(ev), flush=True)
```
Point at it with `HERMES_CLAUDE_LOCAL_BIN=/abs/path/to/fake_claude`.

### B. Integration tests (only if real `claude` is installed AND logged in)
Gate on `claude_local_available()`. Run a real turn:
```bash
uv run hermes -p "Hello, confirm you're working" --provider-ish-selection
# (use whatever the Phase-3 selection mechanism is; pin claude-local in config)
```
Verify: response streams; **no `ANTHROPIC_API_KEY` in env**; the call shows up
in `claude` usage (subscription), not API credits. Capture logs.

### C. Edge cases
- `claude` not installed → clear `ClaudeNotInstalledError` surfaced to user.
- Auth expired → message tells user to run `claude login`.
- Long prompt → no stdin deadlock (stdin is fed on a thread — confirm).
- `HERMES_CLAUDE_LOCAL_TIMEOUT=2` on a slow mock → `ClaudeTimeoutError`.
- Interrupt mid-stream → `InterruptedError` (the `interrupt_check` hook).

---

## Phase 5 — Documentation & cleanup

Deliverable: PR-ready docs.
- Update `README.md`: add `claude-local` to the providers list; quickstart
  **"Using your Claude Code subscription with Hermes"** (install `claude`,
  `claude login`, select the provider, run).
- Note the **provenance**: technique ported from Paperclip's
  `packages/adapters/claude-local/` (`execute.ts` + `parse.ts`).
- Final review: run the repo linter/formatter and full test suite
  (`uv run pytest` or the project's standard); ensure no import cycles; confirm
  the 4 core edits are minimal and additive.
- Open a PR. Suggested title: `feat(providers): claude_local — route turns
  through the Claude CLI subprocess (subscription auth)`. End the PR body with
  the Claude Code attribution footer.

---

## Design constraints to preserve (decided with the user — do not change)
- **Text-only v1.** Hermes does **not** round-trip tool calls. The subprocess
  uses its **own native tools directly** (Read/Edit/Bash) in the working dir;
  only final assistant text returns to Hermes. That's why
  `--dangerously-skip-permissions` is the default (headless `--print` can't
  answer permission prompts) — keep it overridable via
  `HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS=0`.
- **Stateless `--print` per turn.** Hermes owns history; no `--resume`.
- **Subscription auth only.** Never inject `ANTHROPIC_API_KEY`; rely on
  `~/.claude/` (honor `CLAUDE_CONFIG_DIR`).
- **Keep core edits minimal & additive.** The whole point of the hybrid
  approach (see `CLAUDE_LOCAL_PLAN.md`) is that existing api_modes are untouched.

## Quick self-test that current code still works (run first on the cloud box)
```bash
cd <repo>
uv run python -c "
import agent.transports as T, providers
assert T.get_transport('claude_local').api_mode == 'claude_local'
assert providers.get_provider_profile('claude_local').name == 'claude-local'
from agent.claude_local_runtime import build_claude_args, build_prompt_from_messages
print('Phase 1-2 intact')
"
```
