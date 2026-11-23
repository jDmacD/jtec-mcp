# jtec-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP
client) **workflow-level tools** for self-hosted homelab services.

Raw REST APIs make agents do a lot of plumbing: start a search, poll it, page
through 160 KB of JSON, group files, rank them. jtec-mcp does that plumbing and
returns a compact answer, one tool call per step of a real task.

## Tools

| Service | Tool | What it does |
| --- | --- | --- |
| [slskd](https://github.com/slskd/slskd) | `slskd_search_album` | Searches Soulseek, groups results into peer folders (one peer's copy of one edition) and ranks them: most expected tracks (by title when given a track list), lossless first, then free slot, queue and speed. |
| | `slskd_downloads` | Download transfers summarised per peer and folder, with unfinished files listed. |
| | `slskd_conversations` | Private chats with their latest messages, e.g. anti-bot challenges from peers. |

Tools carry MCP annotations (`readOnlyHint`, ...) so clients can allow the
read-only ones without prompting. Tools are only registered for services that
are configured *and* whose secret is present.

## How secrets work

The server never talks to a secret store. It reads API keys from environment
variables, which [secretspec](https://secretspec.dev) fills when it starts the
server:

```
jtec-mcp  =  secretspec run --file secretspec.toml -- jtec-mcp-server
```

- `secretspec.toml` declares the secrets (all optional): `SLSKD`, `AURRAL`, ...
- `SECRETSPEC_PROVIDER` picks the backend: Bitwarden Secrets Manager
  (`bws://<project-uuid>`, needs the `bws` CLI on `PATH` and `BWS_ACCESS_TOKEN`),
  sops, a keyring, 1Password, Vault, ... Lookups are by key name.
- A secret may be the bare key, or a JSON object with the key as one field
  (`key_field` in the config).
- Use your own declarations with `JTEC_MCP_SECRETSPEC=/path/to/secretspec.toml`.
- secretspec asks agents for a reason when reading secrets; the wrapper sets
  `SECRETSPEC_REASON="jtec-mcp server"` unless you set one.

## Configuration

`~/.config/jtec-mcp/config.toml` (or `JTEC_MCP_CONFIG`) says where services
are. It holds no secrets; see [`config.example.toml`](config.example.toml).

```toml
[services.slskd]
url = "https://slskd.example.lan"
key_field = "SLSKD_API_KEY"
```

## Install

With Nix (packaged from `uv.lock` by [uv2nix](https://github.com/pyproject-nix/uv2nix)):

```sh
nix run github:jDmacD/jtec-mcp           # the secretspec-wrapped server
```

Register it with Claude Code for every project:

```sh
claude mcp add --scope user jtec -- nix run github:jDmacD/jtec-mcp
```

(or point it at a `jtec-mcp` installed from the flake's `packages.default`).
`SECRETSPEC_PROVIDER` and the provider's credentials must be in the server's
environment.

## Development

```sh
nix develop          # Python 3.13, uv, secretspec, editable install
pytest
ruff format && ruff check
```

Dependencies are managed with uv (`uv add ...`, `uv lock`); the flake builds
from `uv.lock`, so there is nothing else to update.

### Adding a service

1. `src/jtec_mcp/<service>/client.py`: a thin async client over the API.
2. `src/jtec_mcp/<service>/tools.py`: `register(server, client)`. Shape tools
   around tasks, prefix names with the service, return compact JSON, set
   annotations honestly.
3. Register it in `server.py`, declare its secret in `secretspec.toml`, and add
   it to `config.example.toml`.

## License

MIT
