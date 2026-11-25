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
| [Aurral](https://github.com/lklynet/aurral) (music library) | `aurral_album` | One album at a glance (by id, MusicBrainz ID or album link): what's missing, which folders hold its files, and other albums with the same title, the signs of a mixed or split album. |
| | `aurral_import_folder` | Bring a downloaded folder into the library (hardlinked by default); checks only, unless told to go ahead. |
| | `aurral_album_jobs` | An album's download jobs with each one's source and Soulseek peer: whether it's being pieced together from many peers, and what's still running or blocked. |
| | `aurral_search` | Find artists and albums (with their MusicBrainz IDs) and whether they're already in the library. |
| | `aurral_set_monitored`, `aurral_set_edition`, `aurral_cancel_album_downloads`, `aurral_refresh` | The steps around an import: pause monitoring, pick the edition, stop Aurral's own downloads (including old jobs the album cancel misses), rescan. |
| MusicBrainz (via [Lidarr's metadata server](https://api.lidarr.audio), no key) | `metadata_album` | An album's editions and track list with durations: the titles to search Soulseek for, and the edition a downloaded copy matches. |
| [Sonarr](https://sonarr.tv), [Radarr](https://radarr.video), Lidarr, Prowlarr, Whisparr, Chaptarr | `arr_status` | Version, health warnings, queue counts and free space, for one app or all of them: the check after an upgrade. |
| | `arr_queue` | What's downloading, with each download's state in qBittorrent alongside; stalled torrents get a swarm check (e.g. everyone stuck at 64% behind a seed that never sends). |
| | `arr_item` | Find a series, film, artist or author, with its files and recent history: grabs, imports, failures and why. |
| | `arr_queue_remove` | Remove a stuck or fake download, optionally blocklisting it and searching for a replacement; dry run by default. |
| | `arr_search` | Search for a series (or one season or episode), film, artist or author. |
| | `arr_set_monitored` | Monitor or unmonitor items in bulk (Lidarr: their albums too); dry run by default. |
| | `prowlarr_indexers`, `prowlarr_test`, `prowlarr_set_indexer` | Which indexers are failing and since when; test them (which also ends a failure backoff); switch one on or off. |
| [qBittorrent](https://www.qbittorrent.org) | `qbittorrent_torrent` | Any torrent's state, peers, availability and trackers, with a plain-language diagnosis of why it's stuck. |
| [Jellyfin](https://jellyfin.org) | `jellyfin_sessions` | What's playing, on which device, and whether it's direct playing or transcoding, and why. |
| | `jellyfin_playback_check` | Will this film or episode (or a whole season) direct play on the TV? Bitrate, codecs, HDR, audio passthrough and subtitles, with tips such as a better audio track. |
| [Bazarr](https://www.bazarr.media) | `bazarr_missing` | Films and episodes still missing subtitles, items Bazarr ignores because they have no language profile, and throttled providers. |
| | `bazarr_subtitle_search`, `bazarr_download_subtitle` | Search the subtitle providers for one film or episode and download a numbered candidate. |
| [OPNsense](https://opnsense.org) (firewall, DNS, DHCP) | `opnsense_dns_check` | Would Unbound's blocklist block these domains, and which list and policy. |
| | `opnsense_dns_queries` | What one device looked up, grouped by domain, blocked first: did the blocklist get in the way, or is the device resolving elsewhere (VPN, web filter, DoH)? |
| | `opnsense_client` | Find a device by IP, MAC or hostname: its DHCP lease and vendor, reservation and DNS host overrides. |
| | `opnsense_firewall_log` | Recent firewall-log entries for a device grouped into flows, telling refused new connections apart from harmless out-of-state packets. |
| | `opnsense_pin_host` | Give a device a fixed address and `.lan` name (DHCP reservation plus DNS override with PTR); dry run by default. |

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

### An OPNsense API user

Create a separate OPNsense user for jtec-mcp (System ▸ Access ▸ Users) with
no shell or GUI password, give it these privileges, and create an API key for
it. OPNsense privileges are per page, so they include writing those pages
(Unbound settings and Kea DHCPv4); there is no read-only variant.

| Privilege | Used by |
| --- | --- |
| Services: Unbound (`page-services-unbound`) | blocklist test, query log, host overrides, applying Unbound |
| Services: DHCP: Kea(v4) (`page-dhcp-kea-v4`) | leases, reservations, subnets, applying Kea |
| Diagnostics: Logs: Firewall: Live View (`page-diagnostics-logs-firewall-dynamic`) | firewall log |

Store the downloaded `apikey.txt` content (or `{"key": ..., "secret": ...}`)
as the `OPNSENSE` secret.

## Contributing

`nix develop` gives you a shell with everything needed; `pytest` runs the tests.
Dependencies are managed with [uv](https://docs.astral.sh/uv/), and the Nix
package is built from `uv.lock` by [uv2nix](https://github.com/pyproject-nix/uv2nix).
[`CLAUDE.md`](CLAUDE.md) describes the code layout and conventions (it's written
for coding agents, but it's the most complete guide for people too).

## License

[MIT](LICENSE)
