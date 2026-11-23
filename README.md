# jtec-mcp

Tools that let an AI assistant such as Claude run a self-hosted homelab's
services for you: find an album on Soulseek, check how downloads are going,
answer a peer's chat, and (soon) import music into Aurral.

It is an [MCP](https://modelcontextprotocol.io) server. Rather than handing the
assistant raw REST APIs, each tool does one real step of a task and returns a
short answer. "Find me a complete lossless copy of this album" is one tool
call, not a dozen requests and a page of JSON.

## What it can do

| Service | Tool | |
| --- | --- | --- |
| [slskd](https://github.com/slskd/slskd) (Soulseek) | `slskd_search_album` | Search for an album and list complete copies, best first: all tracks present, lossless, and a peer who can send it now. |
| | `slskd_downloads` | How downloads are going, per peer and album folder. |
| | `slskd_conversations` | Unread private messages, such as peers asking a question before they share. |
| | `slskd_download_folder` | Download one of the search's folders (by its number), so the whole album comes from one peer. |
| | `slskd_clear_downloads` | Tidy finished (or failed, or stuck) entries from the transfer list; dry run by default. |
| | `slskd_reply`, `slskd_acknowledge` | Answer a peer's message (e.g. an anti-bot question), or mark it read. |

More services are planned; each one only appears if you configure it.

Read-only tools are marked as such, so you can let your assistant use them
freely and keep approval prompts for tools that change things.

## Quick start

You need [Nix](https://nixos.org) with flakes. Everything else (Python, the
secret-manager CLIs) comes with the package.

1. **Say where your services are**, in `~/.config/jtec-mcp/config.toml`:

   ```toml
   [services.slskd]
   url = "https://slskd.example.lan"
   key_field = "SLSKD_API_KEY"   # only if the secret is a JSON object
   ```

   This file holds no secrets. See [`config.example.toml`](config.example.toml).

2. **Store the API keys** in a secret manager that
   [secretspec](https://secretspec.dev) supports, such as Bitwarden Secrets
   Manager, sops, 1Password, Vault or your OS keyring. By default each key is
   looked up under the service's name (`slskd`, `aurral`); it can be the bare
   key, or a JSON object containing it.

3. **Tell secretspec where to look**, in the server's environment:

   ```sh
   export SECRETSPEC_PROVIDER="bws://<your-project-uuid>"   # Bitwarden example
   export BWS_ACCESS_TOKEN=...                              # the provider's own login
   ```

4. **Add it to Claude Code** (available in every project):

   ```sh
   claude mcp add --scope user jtec -- nix run github:jDmacD/jtec-mcp
   ```

   Or install `packages.default` from this flake and register `jtec-mcp`.

### With Home Manager

The flake has a Home Manager module that writes the config, reads the
provider's token from a file at startup (so sops-nix or agenix secrets work)
and registers the server with Claude Code:

```nix
{
  imports = [ inputs.jtec-mcp.homeManagerModules.default ];

  services.jtec-mcp = {
    enable = true;
    settings.services.slskd = {
      url = "https://slskd.example.lan";
      key_field = "SLSKD_API_KEY";
    };
    secretspec.provider = "bws://<project-uuid>";
    secretFiles.BWS_ACCESS_TOKEN = config.sops.secrets.bws_token.path;
  };
}
```

## How secrets are handled

jtec-mcp never talks to your secret manager itself. The `jtec-mcp` command runs
`secretspec run`, which fetches the keys listed in
[`secretspec.toml`](secretspec.toml) and starts the server with them as
environment variables. That means:

- you can use any secretspec provider, and switch providers without code changes;
- a missing key just means that service's tools aren't offered;
- secretspec records why secrets were read (it asks automated tools for a
  reason); jtec-mcp gives "jtec-mcp server" unless you set `SECRETSPEC_REASON`.

To use different key names, copy `secretspec.toml`, change the `ref` entries,
and set `JTEC_MCP_SECRETSPEC` to your copy.

## Contributing

`nix develop` gives you a shell with everything needed; `pytest` runs the tests.
Dependencies are managed with [uv](https://docs.astral.sh/uv/), and the Nix
package is built from `uv.lock` by [uv2nix](https://github.com/pyproject-nix/uv2nix).
[`CLAUDE.md`](CLAUDE.md) describes the code layout and conventions (it's written
for coding agents, but it's the most complete guide for people too).

## License

[MIT](LICENSE)
