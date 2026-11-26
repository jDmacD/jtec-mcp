"""Servarr write tools: remove a queue item, search for an item, change
monitoring. Each changes the app, so each has a dry run or a narrow scope."""

from __future__ import annotations

import asyncio
import json
import re
import time

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.arr.client import ArrClient
from jtec_mcp.errors import ToolError

REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
SEARCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
MONITOR = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
EPISODE = re.compile(r"^s(\d+)(?:e(\d+))?$", re.IGNORECASE)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def _wait_command(c: ArrClient, cmd: dict, wait_s: float, poll_s: float = 2) -> dict:
    deadline = time.monotonic() + wait_s
    while cmd.get("status") not in ("completed", "failed", "aborted", "cancelled"):
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(poll_s)
        cmd = await c.command_status(cmd["id"])
    return {
        "command_id": cmd.get("id"),
        "command": cmd.get("name"),
        "status": cmd.get("status"),
        "message": cmd.get("message"),
    }


def register(server: MCPServer, clients: dict[str, ArrClient], entry) -> None:
    """`entry` turns a raw queue record into the summary arr_queue shows."""
    queue_apps = {n: c for n, c in clients.items() if c.has_queue}

    def get(app: str, pool: dict[str, ArrClient]) -> ArrClient:
        c = pool.get(app.lower())
        if c is None:
            raise ToolError(f"unknown app {app!r}; usable here: {', '.join(pool)}")
        return c

    @server.tool(annotations=REMOVE, structured_output=False)
    async def arr_queue_remove(
        app: str,
        queue_id: int,
        blocklist: bool = True,
        remove_from_client: bool = True,
        search: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Remove a download from a Servarr app's queue (ids from arr_queue).

        Dry run by default: shows the entry and what would happen. With
        `dry_run=false`: `remove_from_client` deletes the torrent and its data
        from qBittorrent; `blocklist` stops the arr grabbing the same release
        again (use it for fakes and dead swarms); `search` starts a search for
        a replacement. Removing without blocklisting lets the arr grab the
        same release on its next search.
        """
        c = get(app, queue_apps)
        record = next((r for r in await c.queue() if r.get("id") == queue_id), None)
        if record is None:
            raise ToolError(f"{c.label} has no queue item {queue_id}; see arr_queue")
        summary = entry(c.name, record)
        plan = {
            "remove_from_client": remove_from_client,
            "blocklist": blocklist,
            "search_replacement": search,
        }
        if dry_run:
            return _compact({"dry_run": True, "would_remove": summary, "with": plan})
        await c.queue_remove(queue_id, remove_from_client, blocklist, search)
        return _compact({"removed": summary, "with": plan})

    @server.tool(annotations=SEARCH, structured_output=False)
    async def arr_search(app: str, item_id: int, episode: str = "", wait_s: int = 30) -> str:
        """Start a search in a Servarr app for one library item (ids from arr_item).

        Searches the whole item: a series, movie, artist or author. For Sonarr,
        `episode` narrows it to a season (`S02`) or an episode (`S02E03`).
        Searches only grab releases for monitored items. Waits up to `wait_s`
        for the command to finish and returns its status; check arr_queue for
        what was grabbed.
        """
        c = get(app, queue_apps)
        name, field, as_list = c.app.search
        if episode:
            if c.name != "sonarr":
                raise ToolError("`episode` only applies to Sonarr")
            m = EPISODE.match(episode.strip())
            if not m:
                raise ToolError("episode must look like S02 or S02E03")
            season = int(m.group(1))
            if m.group(2) is None:
                cmd = await c.command("SeasonSearch", seriesId=item_id, seasonNumber=season)
            else:
                number = int(m.group(2))
                eps = [
                    e["id"]
                    for e in await c.episodes(item_id)
                    if e.get("seasonNumber") == season and e.get("episodeNumber") == number
                ]
                if not eps:
                    raise ToolError(f"series {item_id} has no S{season:02d}E{number:02d}")
                cmd = await c.command("EpisodeSearch", episodeIds=eps)
        else:
            cmd = await c.command(name, **{field: [item_id] if as_list else item_id})
        return _compact(await _wait_command(c, cmd, wait_s))

    editable = {n: c for n, c in queue_apps.items() if c.app.editor}

    @server.tool(annotations=MONITOR, structured_output=False)
    async def arr_set_monitored(
        app: str,
        monitored: bool,
        item_ids: list[int] | None = None,
        all_items: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Monitor or unmonitor library items (series, movies or artists) in bulk.

        Give `item_ids` (from arr_item) or `all_items=true`. Dry run by
        default: lists what would change. For Lidarr, unmonitoring also
        unmonitors the artists' albums and sets "monitor new albums" to none;
        albums keep their own flag, so an unmonitored artist with monitored
        albums still gets searched. Not available for Chaptarr, which
        monitors ebooks and audiobooks separately; use
        chaptarr_set_book_monitored.
        """
        c = get(app, editable)
        if not item_ids and not all_items:
            raise ToolError("give item_ids or all_items=true")
        items = await c.library()
        title = c.app.title_field
        chosen = items if all_items else [i for i in items if i.get("id") in set(item_ids)]
        missing = set(item_ids or []) - {i.get("id") for i in chosen}
        if missing:
            raise ToolError(f"{c.label} has no items {sorted(missing)}")
        change = [i for i in chosen if bool(i.get("monitored")) != monitored]
        out = {
            "monitored": monitored,
            "items": len(chosen),
            "changing": [f"{i.get(title)} ({i.get('id')})" for i in change][:50],
        }
        albums = []
        if c.name == "lidarr" and not monitored:
            ids = {i["id"] for i in chosen}
            albums = [
                a["id"] for a in await c.albums() if a.get("artistId") in ids and a.get("monitored")
            ]
            out["albums_unmonitoring"] = len(albums)
        if dry_run:
            return _compact({"dry_run": True, **out})
        await c.set_monitored([i["id"] for i in chosen], monitored)
        if albums:
            await c.set_albums_monitored(albums, False)
        return _compact(out)
