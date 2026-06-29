# CLAUDE_LOCAL_CONFIG.md — Selecting & configuring `claude-local`

The `claude-local` provider routes each Hermes turn through the local `claude`
CLI subprocess (Claude Code) instead of the Anthropic HTTP API, so usage bills
against your **Claude Code subscription** (OAuth in `~/.claude/`) — **no
`ANTHROPIC_API_KEY`**.

This page covers the prerequisite, the two ways to select the provider, and the
environment-variable knobs.

---

## Prerequisite — install & log in

```bash
# Install Claude Code (see https://docs.claude.com/claude-code for all methods)
npm install -g @anthropic-ai/claude-code

# Sign in with your subscription (OAuth — stored in ~/.claude/)
claude login
```

Confirm the CLI is reachable:

```bash
claude --version
```

If `claude` is on your `PATH`, Hermes finds it automatically. To point at a
binary elsewhere, set `HERMES_CLAUDE_LOCAL_BIN` (see the table below).

---

## Selecting the provider

There is **no API-key step** — the subprocess authenticates with Claude's own
credential store. There are three equivalent ways to select it.

### 1. Interactive picker

```bash
hermes model
```

Choose **"Claude Local (CLI subprocess)"** from the list. You'll be asked to
pick a model (e.g. `claude-opus-4-8`); you will **not** be asked for an API key.

### 2. One-shot / chat flag

```bash
hermes -z "Hello, confirm you're working" --provider claude-local -m claude-opus-4-8
```

`--provider` accepts the canonical name or any alias: `claude-local`,
`claude_local`, `claude-cli`, `claude-sub`, `claude-code-local`.

### 3. Config file (`~/.hermes/config.yaml`)

Pin the provider and a default model directly:

```yaml
model:
  provider: claude-local
  default: claude-opus-4-8
  # api_mode is optional — the runtime always resolves claude-local to
  # "claude_local". Writing it just makes the config self-describing.
  api_mode: claude_local
```

After this, plain `hermes` / `hermes -z "…"` runs through the subprocess with no
further flags.

> Mid-session you can also switch with `/model claude-local:claude-opus-4-8`.

---

## Environment variables

All optional; sensible defaults.

| Env var | Default | Meaning |
|---|---|---|
| `HERMES_CLAUDE_LOCAL_BIN` | `claude` | Path/name of the CLI binary. |
| `HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS` | `1` (on) | Pass `--dangerously-skip-permissions` so the subprocess's native write/bash tools run headlessly. Set `0` to restrict to no-permission tools (then use `HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS`). |
| `HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS` | — | Comma-separated `--allowedTools` list, used when skip-permissions is `0` (e.g. `Read,Grep`). |
| `HERMES_CLAUDE_LOCAL_MAX_TURNS` | — | `--max-turns` cap for the subprocess agent loop. |
| `HERMES_CLAUDE_LOCAL_TIMEOUT` | `0` (none) | Per-turn wall-clock budget (seconds). On expiry the turn fails with a clear timeout error. |
| `HERMES_CLAUDE_LOCAL_GRACE` | `20` | Grace seconds after a terminal `result` event before force-kill. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Honored and passed through to the child — the standard Claude credential dir. Use it to point at an alternate logged-in profile. |

### Why `--dangerously-skip-permissions` is the default

The subprocess runs headlessly (`claude --print`), which cannot answer
interactive permission prompts. Skipping permissions lets Claude use its native
Read/Edit/Bash tools directly in the working directory. Set
`HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS=0` to opt out and constrain the toolset via
`HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS`.

---

## Design notes (v1)

- **Text-only.** Hermes does not round-trip structured tool calls through the
  CLI. The subprocess executes tools itself; only the final assistant text
  returns to Hermes. Image/content blocks in the prompt are flattened to text.
- **Stateless per turn.** Hermes owns the conversation history; the full
  transcript is replayed into one `claude --print` invocation each turn (no
  `--resume`).
- **Subscription auth only.** `ANTHROPIC_API_KEY` is never injected — auth comes
  from `~/.claude/` (honoring `CLAUDE_CONFIG_DIR`).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `The 'claude' CLI was not found on PATH` | Install Claude Code, or set `HERMES_CLAUDE_LOCAL_BIN` to its absolute path. |
| `Claude is not authenticated. Run 'claude login'` | Your subscription session expired/missing — run `claude login` (or `claude setup-token`). |
| Turn hangs on a long task | Set `HERMES_CLAUDE_LOCAL_TIMEOUT=<seconds>` to bound it. |
| Native edits/bash refused | `HERMES_CLAUDE_LOCAL_SKIP_PERMISSIONS` is `0` and the tool isn't in `HERMES_CLAUDE_LOCAL_ALLOWED_TOOLS`. Set it to `1`, or add the tool. |
