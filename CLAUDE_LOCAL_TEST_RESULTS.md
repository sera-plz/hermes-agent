# CLAUDE_LOCAL_TEST_RESULTS.md — Phase 4

Validation of the `claude_local` provider end to end. No real `claude` binary
was used for the automated suite — a mock executable emits genuine `stream-json`
so the parser, streaming, usage mapping, and error paths run without spending
subscription tokens.

## Environment

- Project run via **`uv`**.
- **Test runner gotcha:** `uv run pytest` launches pytest from the isolated
  *uv-tool* environment, which does **not** have the project dependencies
  (`yaml`, `httpx`, …) → spurious `ModuleNotFoundError`. Run the suite through
  the project interpreter instead:
  ```bash
  uv sync --extra dev            # once: install pytest et al. into .venv
  uv run python -m pytest tests/test_claude_local.py -q
  ```

---

## A. Unit tests — `tests/test_claude_local.py`

```bash
$ uv run python -m pytest tests/test_claude_local.py -q
.....................................                                    [100%]
37 passed in 2.91s
```

Coverage (all **PASS**):

| Area | Cases |
|---|---|
| `build_prompt_from_messages` | single-user verbatim; multi-turn `User:/Assistant:` transcript; system returned separately; multiple system messages concatenated; list/vision content flattened to text (image block dropped) |
| `build_claude_args` | core flags + `--model`; `--effort`/`--max-turns`; empty system omitted; skip-permissions ON by default; `SKIP_PERMISSIONS=0` → `--allowedTools` path; `SKIP_PERMISSIONS=0` with no allowlist → neither flag |
| `run_claude_local` (mock binary) | happy path → content + usage (`input→prompt`, `cache_read→cached`, `output→completion`) + streaming deltas + `on_first_delta`; `error_max_turns` → `finish_reason="length"`; auth stderr → `ClaudeAuthRequiredError`; missing binary → `ClaudeNotInstalledError`; non-zero exit w/o result → `ClaudeLocalError`; timeout → `ClaudeTimeoutError`; interrupt → `InterruptedError`; 500 KB prompt → no stdin deadlock |
| `detect_login_required` | positive (login/not-logged-in/invalid-key/auth-error/oauth-expired) and negative (empty/ok/rate-limit) |
| `claude_local_available` | true when binary present; false when missing |
| `ClaudeLocalTransport` | registered for `claude_local`; `build_kwargs` packs `__claude_local__` options; `normalize_response(mock)` → correct `NormalizedResponse` |
| Selection wiring | `determine_api_mode`, `normalize_provider` aliases, `get_provider`, profile registration, `resolve_runtime_provider`, catalog → **accounts** tab (no API key), appears in `CANONICAL_PROVIDERS` |

---

## B. Integration — full turn through a mock `claude`

A mock binary (`fake_claude`) emits real `stream-json` then exits 0. Selected
via both `--provider` and config file; `ANTHROPIC_API_KEY` unset throughout.

```bash
# config.yaml: model.provider=claude-local, model.default=claude-opus-4-8
$ env -u ANTHROPIC_API_KEY -u ANTHROPIC_TOKEN \
    HERMES_HOME=$H HERMES_CLAUDE_LOCAL_BIN=$MOCK \
    uv run hermes -z "Hello, confirm you're working" --provider claude-local -m claude-opus-4-8
Hello from claude-local — I am working.        # ← mock output, returned by Hermes

# Pure config-file selection (no flags):
$ env -u ANTHROPIC_API_KEY HERMES_HOME=$H HERMES_CLAUDE_LOCAL_BIN=$MOCK uv run hermes -z "second turn"
Hello from claude-local — I am working.
```

This exercises the complete chain: config/`--provider` → `resolve_runtime_provider`
(→ `api_mode=claude_local`) → `AIAgent` (api_mode preserved, no HTTP client) →
`build_api_kwargs` → `interruptible[_streaming]_api_call` → `run_claude_local`
(subprocess) → `normalize_response` → final text. **PASS**, with no
`ANTHROPIC_API_KEY` in the environment (subscription path).

### Interactive picker flow

Simulated selection of the first offered model:

```text
  claude CLI: ✓ found on PATH
  Auth uses your Claude Code subscription (~/.claude) — no API key needed.
Default model set to: claude-opus-4-8 (via Claude Local CLI)
--- config.yaml model section ---
{'default': 'claude-opus-4-8', 'provider': 'claude-local', 'api_mode': 'claude_local'}
runtime api_mode: claude_local | provider: claude-local
```

No API-key prompt; config persisted correctly; runtime resolves to
`claude_local`. **PASS**.

---

## C. Edge cases

| Case | Mechanism | Result |
|---|---|---|
| `claude` not installed | `HERMES_CLAUDE_LOCAL_BIN=<missing>` | `ClaudeNotInstalledError` with install hint — **PASS** |
| Auth expired | mock emits login error on stderr, exit 1, no result | `ClaudeAuthRequiredError` ("run `claude login`") — **PASS** |
| Long prompt (500 KB) | stdin fed on a daemon thread | no deadlock, normal response — **PASS** |
| `HERMES_CLAUDE_LOCAL_TIMEOUT=1` on a 10 s mock | wall-clock watchdog kills child | `ClaudeTimeoutError` — **PASS** |
| Interrupt mid-stream | `interrupt_check=lambda: True` | `InterruptedError` — **PASS** |

---

## D. Regression — existing suites unaffected

```bash
$ uv run python -m pytest tests/hermes_cli/test_provider_catalog.py \
    tests/hermes_cli/test_api_key_providers.py tests/providers/ \
    tests/agent/transports/ -q
628 passed in 37.80s

$ uv run python -m pytest tests/hermes_cli/test_runtime_provider*.py \
    tests/hermes_cli/test_model_switch*.py \
    tests/hermes_cli/test_model_provider_persistence.py -q
236 passed in 32.04s
```

The four existing modes (`chat_completions`, `codex_responses`,
`anthropic_messages`, `bedrock_converse`) are untouched — all new code is
additive `elif api_mode == "claude_local"` branches plus the selection wiring.

---

## E. Not covered here

- **Live `claude` turn against the real subscription.** Gated on a logged-in
  `claude` being present (`claude_local_available()`); not run in CI to avoid
  spending subscription tokens. To verify locally: `claude login`, then
  `hermes -z "hello" --provider claude-local` and confirm the call shows up in
  `claude` usage (subscription), not Anthropic API credits.
