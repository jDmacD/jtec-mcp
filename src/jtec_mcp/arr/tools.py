"""Servarr MCP tools (Sonarr, Radarr, Lidarr, Prowlarr, Whisparr, Chaptarr):
status and health, the download queue joined with qBittorrent, and an item's
details and history. One set of tools covers every configured app; each takes
an `app` argument."""

from __future__ import annotations

import asyncio
import json

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.arr.client import ArrClient
from jtec_mcp.errors import ToolError
from jtec_mcp.qbittorrent import swarm
from jtec_mcp.qbittorrent.client import QbittorrentClient
from jtec_mcp.qbittorrent.tools import inspect

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# History `data` fields worth showing. Others are left out on purpose: a grab's
# downloadUrl carries Prowlarr's API key in its query string.
HISTORY_DATA = (
    "reason",
    "message",
    "indexer",
    "releaseGroup",
    "size",
    "downloadClientName",
    "droppedPath",
    "importedPath",
    "releaseType",
    "publishedDate",
)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _gb(n) -> float | None:
    try:
        return round(int(n) / 1e9, 2)
    except (TypeError, ValueError):
        return None


def _sxe(ep: dict | None) -> str:
    if not ep:
        return ""
    return f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"


def _queue_item(r: dict) -> str:
    """What the queue entry is for, in the app's own terms."""
    if s := r.get("series"):
        return f"{s.get('title')} {_sxe(r.get('episode'))}".strip()
    if m := r.get("movie"):
        return f"{m.get('title')} ({m.get('year')})"
    if a := r.get("artist"):
        return f"{a.get('artistName')} - {(r.get('album') or {}).get('title')}"
    if a := r.get("author"):
        return f"{a.get('authorName')} - {(r.get('book') or {}).get('title')}"
    return "unknown (not matched to the library)"


def _queue_entry(app: str, r: dict) -> dict:
    size, left = r.get("size") or 0, r.get("sizeleft") or 0
    messages = [
        f"{m.get('title')}: {'; '.join(m.get('messages') or [])}".rstrip(": ")
        for m in r.get("statusMessages") or []
    ]
    if r.get("errorMessage"):
        messages.append(r["errorMessage"])
    return {
        "app": app,
        "queue_id": r.get("id"),
        "item": _queue_item(r),
        "release": r.get("title"),
        "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
        "cf_score": r.get("customFormatScore"),
        "status": r.get("status"),
        "tracked": f"{r.get('trackedDownloadState')}/{r.get('trackedDownloadStatus')}",
        "messages": messages or None,
        "progress_pct": round(100 * (size - left) / size, 1) if size else None,
        "size_gb": _gb(size) if size else None,  # 0 until the client knows
        "timeleft": r.get("timeleft"),
        "added": r.get("added"),
        "indexer": r.get("indexer"),
        "client": r.get("downloadClient"),
        "hash": (r.get("downloadId") or "").lower() or None,
    }


# Bookkeeping events: a library-wide rename or retag writes one per file and
# buries the grabs and imports.
QUIET_EVENTS = {
    "episodeFileRenamed",
    "movieFileRenamed",
    "trackFileRenamed",
    "trackFileRetagged",
    "bookFileRenamed",
    "bookFileRetagged",
}


def _history_entry(h: dict) -> dict:
    data = h.get("data") or {}
    out = {
        "date": h.get("date"),
        "event": h.get("eventType"),
        "source": h.get("sourceTitle"),
        "quality": ((h.get("quality") or {}).get("quality") or {}).get("name"),
        "cf_score": h.get("customFormatScore"),
    }
    if ep := h.get("episode"):
        out["episode"] = _sxe(ep)
    out.update({k: data[k] for k in HISTORY_DATA if data.get(k)})
    if "size" in out:
        out["size"] = _gb(out["size"])
    return out


def _item_summary(client: ArrClient, i: dict, profiles: dict[int, str]) -> dict:
    out = {
        "id": i.get("id"),
        "title": i.get(client.app.title_field),
        "year": i.get("year"),
        "monitored": i.get("monitored"),
        "status": i.get("status"),
        "path": i.get("path"),
        "quality_profile": profiles.get(i.get("qualityProfileId")),
    }
    stats = i.get("statistics") or {}
    if client.app.library == "series":
        out["episodes"] = f"{stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)}"
    elif client.app.library == "movie":
        out["has_file"] = i.get("hasFile")
        if f := i.get("movieFile"):
            out["file"] = {
                "name": f.get("relativePath"),
                "quality": ((f.get("quality") or {}).get("quality") or {}).get("name"),
                "size_gb": _gb(f.get("size")),
                "languages": [lang.get("name") for lang in f.get("languages") or []],
                "release_group": f.get("releaseGroup"),
            }
    elif client.app.library == "artist":
        out["tracks"] = f"{stats.get('trackFileCount', 0)}/{stats.get('totalTrackCount', 0)}"
    elif client.app.library == "author":
        for kind in ("ebook", "audiobook"):
            s = i.get(f"{kind}Statistics") or {}
            out[kind] = {
                "monitored": i.get(f"{kind}Monitored"),
                "books_with_files": s.get("bookFileCount"),
                "books": s.get("bookCount"),
            }
    out["size_gb"] = _gb(stats.get("sizeOnDisk", i.get("sizeOnDisk")))
    return {k: v for k, v in out.items() if v is not None}


def register(
    server: MCPServer, clients: dict[str, ArrClient], qbit: QbittorrentClient | None = None
) -> None:
    queue_apps = {n: c for n, c in clients.items() if c.has_queue}

    def pick(app: str, pool: dict[str, ArrClient] | None = None) -> dict[str, ArrClient]:
        pool = clients if pool is None else pool
        if not app:
            return pool
        if app.lower() not in pool:
            raise ToolError(f"unknown app {app!r}; configured: {', '.join(pool)}")
        return {app.lower(): pool[app.lower()]}

    async def one_status(name: str, c: ArrClient) -> dict:
        try:
            st, health = await asyncio.gather(c.status(), c.health())
            out = {
                "version": st.get("version"),
                "database": f"{st.get('databaseType')} {st.get('databaseVersion') or ''}".strip(),
                "health": [
                    {"type": h.get("type"), "source": h.get("source"), "message": h.get("message")}
                    for h in health
                ],
            }
            if c.has_queue:
                qs, roots = await asyncio.gather(c.queue_status(), c.root_folders())
                out["queue"] = {
                    k: qs.get(k) for k in ("totalCount", "unknownCount", "errors", "warnings")
                }
                out["root_folders"] = [
                    {
                        "path": r.get("path"),
                        "free_gb": _gb(r.get("freeSpace")),
                        "accessible": r.get("accessible"),
                    }
                    for r in roots
                ]
            return out
        except ToolError as e:
            return {"error": str(e)}

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def arr_status(app: str = "") -> str:
        """Version, database, health warnings, queue counts and root-folder free
        space for one Servarr app, or all of them when `app` is empty.

        The check to run after an upgrade or restart, or to see what's
        unhealthy. An app that can't be reached reports `error` instead of
        failing the others. Prowlarr has no queue or root folders.
        """
        pool = pick(app)
        results = await asyncio.gather(*(one_status(n, c) for n, c in pool.items()))
        return _compact(dict(zip(pool, results)))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def arr_queue(app: str = "", problems_only: bool = False, diagnose: int = 5) -> str:
        """The download queue of one Servarr app, or all of them, joined with
        each download's state in qBittorrent.

        Each entry has the item it's for, the release, the arr's tracked state
        and its status messages (import rejections show up here), plus
        `torrent`: qBittorrent's state, progress, availability, seeds and
        speed. Up to `diagnose` stalled torrents also get a swarm check with
        plain-language `findings` (peers stuck at one percentage, a seed that
        never sends). `problems_only` keeps entries that are stalled, warned,
        failed or have messages. Items the arr couldn't match to its library
        are included as "unknown".
        """
        pool = pick(app, queue_apps)
        queues = await asyncio.gather(*(c.queue() for c in pool.values()))
        entries = [_queue_entry(n, r) for n, q in zip(pool, queues) for r in q]

        torrents: dict[str, dict] = {}
        if qbit and (hashes := [e["hash"] for e in entries if e["hash"]]):
            torrents = {t["hash"]: t for t in await qbit.torrents(hashes=hashes)}
        budget = diagnose
        for e in entries:
            t = torrents.get(e["hash"] or "")
            if t is None:
                continue
            if swarm.is_stalled(t) and budget > 0:
                budget -= 1
                e["torrent"] = await inspect(qbit, t)
            else:
                e["torrent"] = swarm.summarize(t)
            e["torrent"].pop("name", None)
            e["torrent"].pop("hash", None)

        def problem(e: dict) -> bool:
            t = e.get("torrent") or {}
            return bool(
                e["messages"]
                or not e["tracked"].endswith("/ok")
                or t.get("state") in swarm.STALLED_STATES
                or e["status"] in ("failed", "warning", "delay")
            )

        if problems_only:
            entries = [e for e in entries if problem(e)]
        return _compact({"count": len(entries), "queue": entries})

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def arr_item(
        app: str, query: str, history: int = 15, include_renames: bool = False
    ) -> str:
        """Find an item in a Servarr app's library by title or id,
        with its files and recent history.

        `query` is a library id or part of the title (case-insensitive). Sonarr
        lists series, Radarr and Whisparr movies, Lidarr artists, Chaptarr
        authors. With one match (or an exact title match) you get the item
        (monitored, quality profile, path, file or episode counts) and its last
        `history` events, newest first: grabs (indexer, release group),
        imports (dropped and imported paths), failures and deletions with
        their reason. Renames and retags are left out unless
        `include_renames`; `hidden_renames` counts them. Several matches are
        listed for you to pick an id.
        """
        c = pick(app, queue_apps)[app.lower()]
        q = query.strip()
        items, profiles = await asyncio.gather(c.library(), c.quality_profiles())
        title = c.app.title_field
        if q.isdigit():
            matches = [i for i in items if i.get("id") == int(q)]
        else:
            matches = [i for i in items if q.lower() in (i.get(title) or "").lower()]
            exact = [i for i in matches if (i.get(title) or "").lower() == q.lower()]
            matches = exact or matches
        if not matches:
            raise ToolError(f"{c.label} has nothing matching {query!r}")
        if len(matches) > 1:
            return _compact(
                {
                    "matches": [
                        {"id": i.get("id"), "title": i.get(title), "year": i.get("year")}
                        for i in matches[:20]
                    ],
                    "more": max(len(matches) - 20, 0) or None,
                }
            )
        item = matches[0]
        out = _item_summary(c, item, profiles)
        if history > 0:
            events = await c.history(item["id"])
            events.sort(key=lambda h: h.get("date") or "", reverse=True)
            if not include_renames:
                quiet = sum(1 for h in events if h.get("eventType") in QUIET_EVENTS)
                events = [h for h in events if h.get("eventType") not in QUIET_EVENTS]
                out["hidden_renames"] = quiet or None
            out["history"] = [_history_entry(h) for h in events[:history]]
        return _compact(out)
