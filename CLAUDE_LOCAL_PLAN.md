# CLAUDE_LOCAL_PLAN.md — Phase 1: Analysis & Architecture Decision

**Goal:** Route Hermes Agent LLM requests through the Claude CLI subprocess (`claude`) instead of the Anthropic HTTP API, so usage bills against a Claude Code **subscription** (OAuth, `~/.claude/`) rather than Anthropic **API credits**.

**Provenance:** Technique adapted from Paperclip's `claude-local` adapter (`packages/adapters/claude-local/`).

---

## DECISION (TL;DR)

> **HYBRID — minimal, surgical fork.** A *pure* plugin (zero core edits) is **not possible** because Hermes hardcodes the `api_mode → execution-method` dispatch in `agent/chat_completion_helpers.py` (three `if/elif` sites). However, the bulk of the work lives in pluggable layers (a provider profile + an auto-registered transport). The required core change is small and additive: **one new `api_mode` ("claude_local") added to three dispatch branches**, plus one new transport module, plus one provider-profile plugin.
>
> Estimated core footprint: **~3 edited files, ~40–60 added lines** (all additive `elif` branches, no behavior change to existing modes), plus **2 new files** (transport + subprocess runner) and **1 plugin dir**.

Two implementation strategies were evaluated; the recommended one is **Strategy A (new api_mode)**. Strategy B (shim client under `anthropic_messages`) is documented as the rejected alternative.

---

## A. Paperclip `claude_local` Implementation

**Location:** `/Users/wandmagic/Documents/paperclip/packages/adapters/claude-local/`

| Component | File | Key symbol |
|---|---|---|
| Execution orchestration | `src/server/execute.ts` | `buildClaudeArgs()`, `runAttempt()` |
| Response parsing / error classification | `src/server/parse.ts` | `parseClaudeStreamJson()` |
| Auth/credential seeding | `src/server/claude-config.ts` | `prepareClaudeConfigSeed()`, `resolveSharedClaudeConfigDir()` |
| Adapter metadata / config schema | `src/index.ts` | — |

### Subprocess spawn — command & flags

`execute.ts:675-703`:

```typescript
const buildClaudeArgs = (resumeSessionId, attemptInstructionsFilePath) => {
  const args = ["--print", "-", "--output-format", "stream-json", "--verbose"];
  if (resumeSessionId) args.push("--resume", resumeSessionId);
  args.push(...buildClaudeExecutionPermissionArgs({ dangerouslySkipPermissions, targetIsSandbox }));
  if (chrome) args.push("--chrome");
  if (model && (!isBedrockAuth(effectiveEnv) || isBedrockModelId(model))) args.push("--model", model);
  if (effort) args.push("--effort", effort);
  if (maxTurns > 0) args.push("--max-turns", String(maxTurns));
  if (attemptInstructionsFilePath && !resumeSessionId)
    args.push("--append-system-prompt-file", attemptInstructionsFilePath);
  if (effectivePromptBundleAddDir) { args.push("--add-dir", effectivePromptBundleAddDir); ... }
  return args;
};
```

- Binary: `"claude"` (resolved from PATH).
- Core flags we need: **`--print -` (read prompt from stdin, print result)**, **`--output-format stream-json`**, **`--verbose`**, optional **`--model`**, **`--resume <id>`** for session continuity, **`--max-turns`**.
- The prompt is delivered on **stdin** (`stdin: prompt`), not as a `-p "..."` arg. (Note: the original task brief suggested `claude -p "<prompt>" --output-format json`; Paperclip's battle-tested form is `--print - --output-format stream-json` with prompt on stdin — we follow Paperclip.)

### Communication protocol — line-delimited stream-json on stdout

`parse.ts:17-86` — iterate `stdout.split(/\r?\n/)`, JSON-parse each non-empty line. Event types:

- `{"type":"system","subtype":"init","session_id":...,"model":...}` — session/model init
- `{"type":"assistant","message":{"content":[{"type":"text","text":...}]}}` — assistant text blocks
- `{"type":"result","result":"...","usage":{...},"total_cost_usd":...,"session_id":...}` — terminal event

Canonical example (`server/src/__tests__/claude-local-execute.test.ts:69-71`):

```json
{"type":"system","subtype":"init","session_id":"claude-session-1","model":"claude-sonnet"}
{"type":"assistant","session_id":"claude-session-1","message":{"content":[{"type":"text","text":"hello"}]}}
{"type":"result","session_id":"claude-session-1","result":"hello","usage":{"input_tokens":1,"cache_read_input_tokens":0,"output_tokens":1}}
```

### Parsed response shape (`parse.ts`)

```typescript
{ sessionId, model, costUsd, usage: { inputTokens, cachedInputTokens, outputTokens }, summary, resultJson }
```

`usage` maps `input_tokens`, `cache_read_input_tokens`, `output_tokens`. `costUsd` from `total_cost_usd`.

### Credentials — Claude's own store, no API key

`claude-config.ts:84-89` — config dir = `$CLAUDE_CONFIG_DIR` or `~/.claude`. Seeded files (`claude-config.ts:8-14`): `.credentials.json`, `credentials.json`, `settings.json`, `settings.local.json`, `CLAUDE.md`. **No `ANTHROPIC_API_KEY` required** — the CLI uses its own OAuth credentials. For local execution we simply let the child inherit `HOME`/`CLAUDE_CONFIG_DIR`; the remote "seed" machinery is Paperclip-specific and **out of scope** for our local-only provider.

### Process lifecycle & error classification

- **Timeout**: `timeoutSec` + `graceSec` (default 20s grace); timeout → `errorCode:"timeout"` (`execute.ts:277-281, 792-801`).
- **Terminal-result early cleanup**: kill once a `result` event is seen rather than waiting for natural exit (`execute.ts:760-763`).
- **Auth required** (`parse.ts:132-149`): regex on `not logged in / please run claude login` → `errorCode:"claude_auth_required"`.
- **Transient upstream** (`parse.ts:370-391`): `rate limit / overloaded / service unavailable / out of extra usage` → retryable.
- **Unknown session** (`parse.ts:188-197`): `no conversation found with session id` → auto-retry with fresh session (`execute.ts:944-961`).
- **Max turns** (`parse.ts:167-186`): `subtype === "error_max_turns"` → clear session.

---

## B. Hermes Agent Provider Architecture

Three layers — **two are pluggable, one is not**:

```
ProviderProfile  (providers/)            ← PLUGGABLE  (bundled + $HERMES_HOME/plugins/model-providers/)
      │  declares: name, aliases, api_mode, auth_type, env_vars, base_url ...
      ▼
ProviderTransport (agent/transports/)    ← PLUGGABLE-ish  (auto-registered by api_mode; discovery list is hardcoded but register_transport() can be called from a plugin import)
      │  build_kwargs() → dict ;  normalize_response(raw) → NormalizedResponse
      ▼
Execution dispatch (agent/chat_completion_helpers.py)  ← *** HARDCODED if/elif on api_mode ***  ← THE BLOCKER
      │  decides HOW to actually call (anthropic SDK / openai SDK / boto3 / subprocess)
      ▼
NormalizedResponse → conversation loop continues
```

### B1. ProviderProfile (pluggable) — `providers/base.py`, `providers/__init__.py`

Plugins live in `plugins/model-providers/<name>/` (bundled) **or** `$HERMES_HOME/plugins/model-providers/<name>/` (user). Discovery (`providers/__init__.py:102-192`) imports each dir's `__init__.py`, which calls `register_provider(...)` at module level. **User plugins override bundled by name.** Each profile carries:

```python
@dataclass
class ProviderProfile:
    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name / description / signup_url: str
    env_vars: tuple = ()
    base_url: str = ""
    auth_type: str = "api_key"   # api_key | oauth_device_code | oauth_external | copilot | aws_sdk
    ...
```

Reference precedent for non-API-key auth: **Codex** (`plugins/model-providers/openai-codex/__init__.py`) uses `auth_type="oauth_external"`, `env_vars=()`. We mirror this with a new `auth_type` (e.g. `"claude_cli"` / `"subprocess"`).

### B2. ProviderTransport ABC — `agent/transports/base.py`

```python
class ProviderTransport(ABC):
    @property @abstractmethod
    def api_mode(self) -> str: ...
    @abstractmethod
    def convert_messages(self, messages, **kwargs) -> Any: ...
    @abstractmethod
    def convert_tools(self, tools) -> Any: ...
    @abstractmethod
    def build_kwargs(self, model, messages, tools=None, **params) -> Dict[str, Any]: ...
    @abstractmethod
    def normalize_response(self, response, **kwargs) -> NormalizedResponse: ...
    # optional: validate_response, extract_cache_stats, map_finish_reason
```

`NormalizedResponse` (`agent/transports/types.py:79-145`): `content`, `tool_calls`, `finish_reason`, `reasoning`, `usage`, `provider_data`.

Registry (`agent/transports/__init__.py`): `register_transport(api_mode, cls)` / `get_transport(api_mode)`; `_discover_transports()` imports the four built-ins. Adding our module to that import list is a one-line additive edit (or the profile plugin can `import agent.transports.claude_local` to self-register).

### B3. Execution dispatch (NOT pluggable) — `agent/chat_completion_helpers.py`

The hard truth. Three hardcoded `if/elif api_mode == ...` sites:

1. **`build_api_kwargs()`** (`:589-811`) — picks transport & assembles kwargs per mode.
2. **`interruptible_api_call()`** (`:154-288`, non-streaming) — branches to the actual call:
   - `codex_responses` → `agent._run_codex_stream(...)` (HTTP via OpenAI SDK `responses.create`)
   - `anthropic_messages` → `agent._anthropic_messages_create(...)` (Anthropic SDK `messages.create/stream`)
   - `bedrock_converse` → boto3 `client.converse(**kwargs)`
   - else → OpenAI SDK `request_client.chat.completions.create(**kwargs)`
3. **`interruptible_streaming_api_call()`** (`:1673-1794`, streaming) — parallel branches (`codex_responses` delegates to non-stream; `bedrock_converse` → `converse_stream`; anthropic/chat fall through to SDK streaming).

Streaming vs non-streaming is chosen upstream in `agent/conversation_loop.py:1159-1188` (`_use_streaming`), independent of api_mode.

**Critical finding:** *No existing api_mode executes via subprocess.* Codex — the closest "non-standard auth" precedent — is still **HTTP** (`client.responses.create()` in `agent/codex_runtime.py:649-735`), not a subprocess. So there is no existing subprocess execution path to piggyback on; we must add one.

---

## C. Architecture Decision — PLUGIN vs FORK

### Can it be a pure plugin? **No.**
- The provider **profile** can be a plugin ✅
- The **transport** can self-register ✅
- But execution **dispatch** is hardcoded `if/elif` ❌ — there is no extension hook that lets a plugin say "for api_mode X, run *this* callable to produce the response." Without editing `chat_completion_helpers.py`, a new api_mode falls through to the `else` branch and Hermes tries `request_client.chat.completions.create(**kwargs)` against a non-existent HTTP client → failure.

### Decision: **HYBRID (minimal additive fork) — Strategy A: new `api_mode = "claude_local"`**

Add the new mode as additive `elif` branches alongside the existing four. Rationale:
- **Matches the codebase grain.** Every provider family already lives as a branch in these same three functions; we're adding a fifth, not inventing a parallel mechanism.
- **Fully isolated.** All four existing modes are untouched; the new branches only fire when `api_mode == "claude_local"`. Zero regression surface.
- **Keeps logic out of the dispatcher.** The branches are thin (≤10 lines each) and delegate to a dedicated transport + a small subprocess runner module — so the heavy logic still lives in clean, testable, semi-pluggable files.

### Rejected alternative — Strategy B: shim client under `anthropic_messages`
Reuse `api_mode="anthropic_messages"` and inject a **duck-typed fake client** exposing `.messages.create()`/`.messages.stream()` that internally spawns `claude` and returns an Anthropic-shaped `Message`. **Appeal:** in principle zero dispatch edits. **Why rejected:**
- Client construction (`build_anthropic_client` in `agent/anthropic_adapter.py`) is itself selected per provider/auth and is **not plugin-injectable** — so it *still* needs a core hook. No real savings.
- Forcing subprocess semantics through the Anthropic SDK's `stream()` context-manager contract (`stream.get_final_message()`, `stream.text_stream`) is fragile and obscures what's happening.
- Harder to give claude_local-specific behavior (effort flag, `--resume` sessions, CLI-specific error codes) without polluting the anthropic path.

Strategy A is more honest, more isolated, and barely larger.

---

## Implementation Approach Outline (preview of Phase 2)

**New files (the pluggable bulk):**
1. `plugins/model-providers/claude-local/__init__.py` + `plugin.yaml` — register `ProviderProfile(name="claude-local", aliases=("claude-cli","claude-sub"), api_mode="claude_local", auth_type="claude_cli", env_vars=())`. May `import agent.transports.claude_local` to self-register the transport.
2. `agent/transports/claude_local.py` — `ClaudeLocalTransport(ProviderTransport)`:
   - `api_mode` → `"claude_local"`
   - `build_kwargs()` → flatten OpenAI-format `messages` into the stdin prompt string (+ system prompt file), capture `model`, `max_turns`, `effort`; return `{"prompt": ..., "model": ..., "system_prompt": ..., "claude_args": [...]}`.
   - `normalize_response()` → consume `parseClaudeStreamJson`-equivalent output → `NormalizedResponse(content, finish_reason, usage=...)`. (Tool-calls: v1 can be text-only; tool_use blocks are a documented follow-up.)
   - `convert_messages`/`convert_tools` → minimal.
3. `agent/claude_local_runtime.py` — the subprocess runner (Python port of Paperclip's `execute.ts`/`parse.ts`): `subprocess.Popen(["claude","--print","-","--output-format","stream-json","--verbose", ...])`, write prompt to stdin, read stdout line-by-line, parse stream-json (streaming deltas via `on_first_delta`/text callback), classify errors (not-installed → `FileNotFoundError`; auth → "run `claude login`"; timeout), early-terminate on `result`.

**Core edits (the minimal additive fork — 3 sites in `agent/chat_completion_helpers.py` + 1 in transports `__init__`):**
1. `build_api_kwargs()` — add `if agent.api_mode == "claude_local": return transport.build_kwargs(...)`.
2. `interruptible_api_call()` — add `elif agent.api_mode == "claude_local": result["response"] = run_claude_local(api_kwargs)`.
3. `interruptible_streaming_api_call()` — add `if agent.api_mode == "claude_local": ... run_claude_local_stream(api_kwargs, on_first_delta=...)`.
4. `agent/transports/__init__.py::_discover_transports()` — add guarded `import agent.transports.claude_local`.

**Selection / config (Phase 3):**
- Profile aliases make `hermes model --provider claude-local` resolve. Confirm `detect_static_provider_for_model` / model-switch picks up the new profile (it iterates the registry, so registration should suffice).
- Document `CLAUDE_CONFIG_DIR` override and the `claude login` prerequisite.

**Edge cases (Phase 4):** claude not on PATH → actionable error; auth expired → "run `claude login`"; long prompts → timeout config; large outputs → streaming verified line-by-line.

---

## Open questions for checkpoint review
1. **Tool calling:** v1 = text completions only (no function/tool calls round-tripped through the CLI), or do we need to translate `tool_use`/`tool_result` blocks from stream-json into Hermes `ToolCall`s in this pass? (Affects transport scope significantly.)
2. **Session continuity:** wire `--resume <session_id>` (stateful, matches Paperclip) or treat each turn as a fresh `--print` invocation (simpler, stateless)? Hermes already manages full message history, so **stateless `--print` per turn** is likely correct and simpler — confirm.
3. **Provider name:** `claude-local` (Hermes convention is hyphenated dir names) vs the task's `claude_local`. I propose dir/profile `claude-local`, api_mode string `claude_local`. OK?
4. **Accept the minimal core edits** to `agent/chat_completion_helpers.py`? (Required — no pure-plugin path exists.)

---

**Phase 1 complete. Awaiting checkpoint review before Phase 2 (implementation).**
