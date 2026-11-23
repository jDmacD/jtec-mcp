# CLAUDE.md

Guidance for coding agents working on this repository. `README.md` is for
people using the server; this file is for changing it.

## What this is

An MCP server (stdio) exposing workflow-level tools for self-hosted services.
Each service is a subpackage with a thin API client and a `register(server,
client)` that adds its tools. `server.py` registers a service only when it is
configured in `config.toml` *and* its secret is present in the environment.

```
src/jtec_mcp/
  server.py         build() registers services; main() runs stdio
  config.py         config.toml loading; ServiceConfig.api_key() (bare or JSON field)
  slskd/client.py   async httpx client for slskd API v0
  slskd/ranking.py  pure: search responses -> ranked album folders
  slskd/tools.py    the MCP tools
secretspec.toml     secrets the server reads (all optional)
flake.nix           uv2nix package + dev shell; wraps the server in `secretspec run`
```

## Commands

Always work inside the dev shell; it provides Python 3.13 from nixpkgs, uv,
secretspec, bws and sops.

```sh
nix develop --command pytest -q
nix develop --command sh -c 'ruff format . && ruff check .'
nix build .#default            # the secretspec-wrapped `jtec-mcp`
```

- Flakes only see tracked files: `git add` new files before `nix build` or
  `nix develop`, or they are silently missing.
- Add dependencies with `uv add <pkg>` (or `uv add --dev`) inside the shell, then
  commit `uv.lock`. The flake builds from the lock; there is nothing else to edit.
- Never let uv download its own Python (`UV_PYTHON_DOWNLOADS=never` is set in
  the shell): uv-managed CPython does not run on NixOS.
- Unfree packages: only `bws` is allowed (`providerTools` in `flake.nix`). Extend
  the predicate rather than enabling `allowUnfree` wholesale.

## MCP SDK (mcp 2.x)

The SDK changed a lot from 1.x; don't write from 1.x memory.

- `from mcp.server.mcpserver import MCPServer` (FastMCP no longer exists).
- Types come from `mcp_types`, e.g. `from mcp_types import ToolAnnotations`.
- Pydantic fields are snake_case: `read_only_hint`, `structured_content`,
  `is_error`.
- Register tools with `@server.tool(annotations=..., structured_output=False)`
  and return a **compact JSON string** (`_compact()` in `slskd/tools.py`). A
  dict return is pretty-printed, a list is split into one content block per
  item, and structured output adds a duplicate `{"result": ...}` copy.

## Tool design rules

- **One tool per task step**, not per endpoint. Do the polling, paging,
  grouping and ranking in the server; return only what the agent needs to
  decide the next step.
- **Prefix names with the service** (`slskd_...`, `aurral_...`).
- **Docstrings are the tool description** the agent sees: say what it does,
  how results are ordered, and what each optional argument is for.
- **Annotations must be honest.** `read_only_hint=True` only if nothing changes
  on the service. A search that creates a record is not read-only.
- **Write tools come later than read tools**, and anything destructive gets a
  dry-run mode and `destructive_hint=True`.
- Keep pure logic (like `ranking.py`) separate from I/O so it can be unit
  tested without a server.

## Secrets and configuration

- The server never calls a secret store. It reads environment variables named
  by `secret_env` in `config.toml`; the `jtec-mcp` wrapper fills them with
  `secretspec run`.
- New service: declare its secret in `secretspec.toml` with `required = false`
  and a `ref = { item = "<name>" }`, and add it to `config.example.toml`.
- Provider lookups are by key name (the bws provider can't look up by ID and
  can't extract a JSON field; `ServiceConfig.api_key()` does the extraction).
- secretspec refuses agent access without a reason; set `SECRETSPEC_REASON`
  when running it by hand.

## Public repository

This repo is public. Never commit real hostnames, URLs, IPs, project or secret
IDs, usernames or email addresses. Use `example.lan`-style placeholders in
code, tests and docs. A user's real settings live in
`~/.config/jtec-mcp/config.toml`, outside the repo.

## Testing

- Unit tests use `httpx.MockTransport` for clients and call `build({...})` with
  a `ServiceConfig` to check which tools register.
- Async tests use the `anyio` marker with an `anyio_backend` fixture returning
  `"asyncio"`.
- For a live check, drive the built package over stdio with
  `mcp.client.stdio.stdio_client` + `ClientSession`, passing `PATH`, `HOME`,
  `SECRETSPEC_PROVIDER`, the provider's credential and `JTEC_MCP_CONFIG` in
  `StdioServerParameters(env=...)`. Don't print secret values or chat text.

## Adding a service

1. `src/jtec_mcp/<service>/client.py`: async httpx client, constructor takes
   `(ServiceConfig, api_key, transport=None)`.
2. `src/jtec_mcp/<service>/tools.py`: `register(server, client)`.
3. Wire it into `build()` in `server.py`, following the slskd block.
4. Declare the secret in `secretspec.toml`, add it to `config.example.toml`, and
   add a row to the tools table in `README.md`.
5. Tests: client with a mock transport, pure logic, and registration.

## Commits

Conventional commits (`feat:`, `fix:`, `chore:` ...). Renovate keeps `uv.lock`
and `flake.lock` current.
