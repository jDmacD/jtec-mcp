"""qBittorrent MCP tools: inspect a torrent and diagnose a stalled swarm."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.qbittorrent import swarm
from jtec_mcp.qbittorrent.client import QbittorrentClient

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
HASH_LEN = (40, 64)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def inspect(client: QbittorrentClient, t: dict) -> dict:
    """A torrent's summary, plus its swarm diagnosis and trackers."""
    h = t["hash"]
    out = swarm.summarize(t)
    out["swarm"] = swarm.diagnose(t, await client.peers(h))
    out["swarm"].update(swarm.tracker_summary(await client.trackers(h)))
    return out


def register(server: MCPServer, client: QbittorrentClient) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def qbittorrent_torrent(query: str = "", state: str = "", limit: int = 3) -> str:
        """Inspect torrents in qBittorrent and diagnose why they are stuck.

        `query` is an info-hash or a case-insensitive part of the name. Without
        a query, `state` picks torrents by qBittorrent's filter (`stalled`,
        `downloading`, `errored`, ...); with neither, it checks every stalled
        download, including magnets stuck fetching metadata. Up to `limit`
        matches get the full check: peers (how many are seeds, how many sit at
        the same percentage, whether any seed is sending), availability and
        trackers, with plain-language
        `findings` (e.g. a swarm stuck at 64% behind a seed that never sends).
        Further matches are listed by name only. For a download an arr app
        grabbed, arr_queue shows the same check next to the arr's own status.
        """
        q = query.strip()
        if q and len(q) in HASH_LEN and all(c in "0123456789abcdefABCDEF" for c in q):
            found = await client.torrents(hashes=[q])
        elif q or state:
            found = await client.torrents(filter=state or None)
            if q:
                found = [t for t in found if q.lower() in (t.get("name") or "").lower()]
        else:
            # qBittorrent's own stalled filters leave out magnets stuck on metadata.
            found = [t for t in await client.torrents() if swarm.is_stalled(t)]
            if not found:
                return _compact({"torrents": [], "note": "no stalled downloads"})
        if not found:
            raise ToolError(f"no torrent matches {query or state!r}")
        found.sort(key=lambda t: t.get("added_on") or 0, reverse=True)
        checked = [await inspect(client, t) for t in found[: max(limit, 1)]]
        rest = [t.get("name") for t in found[max(limit, 1) :]]
        return _compact({"torrents": checked, "more": rest or None})
