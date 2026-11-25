"""Aurral MCP tools: inspect an album, import a downloaded folder, and the
monitoring/edition/refresh steps around an import."""

from __future__ import annotations

import asyncio
import json
import posixpath
import time
from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.aurral.client import AurralClient, album_ref
from jtec_mcp.aurral.jobs import (
    ACTIVE,
    CANCELLABLE,
    match_album_jobs,
    order_jobs,
    summarise_job,
)
from jtec_mcp.errors import ToolError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
CANCEL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
IMPORT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


async def _wait_refresh(client: AurralClient, job_id, timeout_s: float, poll_s: float = 2) -> str:
    deadline = time.monotonic() + timeout_s
    status = "queued"
    while time.monotonic() < deadline:
        try:
            status = (await client.refresh_status(job_id)).get("status", "unknown")
        except ToolError:
            return "completed"  # finished and purged from the queue
        if status in ("completed", "failed"):
            return status
        await asyncio.sleep(poll_s)
    return status


def register(server: MCPServer, client: AurralClient) -> None:
    async def resolve(album: str) -> dict:
        return await client.album(album_ref(album))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def aurral_search(query: str, limit: int = 5) -> str:
        """Search Aurral's catalogue (MusicBrainz-backed) for artists and albums.

        Returns MBIDs and whether each is already in the library (`in_library`
        can lag behind a recent import; aurral_album is authoritative). Use an album's
        `release_group` with aurral_album (if in the library) or
        metadata_album (for editions and track lists, e.g. to give
        slskd_search_album its `track_titles`).
        """
        d = await client.search(query, limit)
        return _compact(
            {
                "artists": [
                    {
                        "name": a.get("name"),
                        "mbid": a.get("id") if a.get("hasMbid") else None,
                        "disambiguation": a.get("disambiguation") or None,
                        "in_library": a.get("inLibrary"),
                    }
                    for a in d.get("artists") or []
                ],
                "albums": [
                    {
                        "title": a.get("title"),
                        "artist": a.get("artistName"),
                        "release_group": a.get("id"),
                        "type": a.get("primaryType"),
                        "secondary": a.get("secondaryTypes") or None,
                        "date": a.get("releaseDate"),
                        "in_library": a.get("inLibrary"),
                        "library_id": a.get("libraryAlbumId"),
                    }
                    for a in d.get("albums") or []
                ],
            }
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def aurral_album(album: str, show_tracks: bool = True) -> str:
        """Inspect one library album: status, tracks, which folders hold its files,
        and other albums by the same artist with the same title.

        `album` is a library id, a release-group MBID, or an Aurral album URL.
        Files spread over several folders, or `same_title_albums`, are the signs
        of a mixed or split album (e.g. a track filed under another edition).
        Aurral's API does not say which Soulseek peer supplied each file.
        """
        a = await resolve(album)
        album_id = str(a["id"])
        status, tracks, siblings = await asyncio.gather(
            client.album_status(album_id),
            client.tracks(album_id),
            client.artist_albums(str(a["artistId"])),
        )
        available = [t for t in tracks if t.get("hasFile")]
        paths = await asyncio.gather(*(client.track_files(str(t["id"])) for t in available))
        path_by_track = {str(t["id"]): p for t, p in zip(available, paths)}
        folders = Counter(posixpath.dirname(p) for ps in paths for p in ps)
        same_title = [
            {
                "id": s.get("id"),
                "release_group": s.get("releaseGroupMbid"),
                "monitored": s.get("monitored"),
                "path": s.get("path"),
            }
            for s in siblings
            if str(s.get("id")) != album_id and _norm(s.get("title", "")) == _norm(a["title"])
        ]
        out = {
            "id": album_id,
            "artist": a.get("artistName"),
            "title": a.get("title"),
            "release_group": a.get("releaseGroupMbid"),
            "release_date": status.get("releaseDate") or a.get("releaseDate"),
            "monitored": a.get("monitored"),
            "status": status.get("status"),
            "counts": {k: v for k, v in (status.get("counts") or {}).items() if v},
            "folders": dict(folders),
            "same_title_albums": same_title,
        }
        if show_tracks:
            out["tracks"] = [
                {
                    "pos": f"{t.get('discNumber', 1)}.{t.get('trackNumber')}",
                    "title": t.get("title"),
                    "available": t.get("hasFile"),
                    "quality": t.get("quality"),
                    "size_mb": round((t.get("size") or 0) / 1e6, 1),
                    "file": posixpath.basename(path_by_track.get(str(t["id"]), [""])[0] or "")
                    or None,
                }
                for t in tracks
            ]
        return _compact(out)

    @server.tool(annotations=CHANGE, structured_output=False)
    async def aurral_set_monitored(album: str, monitored: bool) -> str:
        """Monitor or unmonitor an album.

        Unmonitor before replacing an album's files by hand, or Aurral starts
        downloading the tracks that go missing. Re-monitor only once the album
        shows every track available, or it queues downloads for the gaps.
        """
        a = await resolve(album)
        await client.set_monitored(str(a["id"]), monitored)
        return _compact({"id": a["id"], "title": a.get("title"), "monitored": monitored})

    async def album_or_title(album: str) -> tuple[dict | None, str | None, str | None]:
        """(library album or None, release-group MBID, title) for an album
        reference, or for a plain title when the album isn't in the library."""
        try:
            ref = album_ref(album)
        except ToolError:
            return None, None, album
        try:
            a = await client.album(ref)
        except ToolError as e:
            if "404" not in str(e) or ref.isdigit():
                raise
            return None, ref, None  # an MBID whose album row is gone
        return a, a.get("releaseGroupMbid"), a.get("title")

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def aurral_album_jobs(
        album: str, artist: str | None = None, active_only: bool = False
    ) -> str:
        """List Aurral's download jobs for one album, with each job's source
        and Soulseek peer: the way to see where an album's files came from, or
        whether it is being pieced together track by track from many peers.

        `album` is a library id, a release-group MBID, an Aurral album URL, or
        an album title (for jobs whose album is no longer in the library).
        Jobs match by release-group MBID or by album title (case- and
        curly-quote-insensitive); `artist` narrows a title match. A box set or
        compilation containing the album is a different album, with its own
        title and MBID. Active jobs come first; `active_only` drops finished
        ones.
        """
        a, rg, title = await album_or_title(album)
        found = match_album_jobs(await client.jobs(), rg, title, artist)
        if active_only:
            found = [j for j in found if j.get("status") in ACTIVE]
        found = order_jobs(found)
        return _compact(
            {
                "album_id": a["id"] if a else None,
                "title": title,
                "release_group": rg,
                "counts": dict(Counter(j.get("status") for j in found)),
                "peers": len({j["remoteUsername"] for j in found if j.get("remoteUsername")}),
                "jobs": [summarise_job(j) for j in found],
            }
        )

    @server.tool(annotations=CANCEL, structured_output=False)
    async def aurral_cancel_album_downloads(album: str, artist: str | None = None) -> str:
        """Cancel Aurral's queued and running downloads for an album.

        Runs Aurral's album cancel, then cancels every remaining pending or
        downloading job that matches the album (as in aurral_album_jobs).
        The album cancel misses jobs no longer linked to the album's tracks,
        such as old request jobs, which otherwise keep re-downloading files.
        `album` is a library id, MBID, album URL or title. Unmonitor the album
        too, or Aurral queues the gaps again. "blocked" jobs (held for review)
        can't be cancelled and are reported as left.
        """
        a, rg, title = await album_or_title(album)
        album_result = await client.cancel(str(a["id"])) if a else None
        active = [j for s in sorted(CANCELLABLE) for j in await client.jobs(s)]
        cancelled, failed = [], []
        for job in match_album_jobs(active, rg, title, artist):
            try:
                await client.cancel_job(job["id"])
                cancelled.append(job["id"])
            except ToolError as e:
                failed.append({"id": job["id"], "error": str(e)})
        left = match_album_jobs(await client.jobs("blocked"), rg, title, artist)
        return _compact(
            {
                "album_id": a["id"] if a else None,
                "title": title,
                "album_cancel": album_result,
                "jobs_cancelled": cancelled,
                "failed": failed,
                "blocked_left": [summarise_job(j) for j in left],
            }
        )

    @server.tool(annotations=CHANGE, structured_output=False)
    async def aurral_set_edition(album: str, release_mbid: str) -> str:
        """Set which release (edition) of the album Aurral expects, by MusicBrainz
        release MBID. Match it to the copy being imported so its track list (count,
        order, durations) is the one the files are matched against."""
        a = await resolve(album)
        await client.set_edition(str(a["id"]), release_mbid)
        return _compact({"id": a["id"], "title": a.get("title"), "release": release_mbid})

    @server.tool(annotations=IMPORT, structured_output=False)
    async def aurral_import_folder(
        source_path: str,
        mode: str = "hardlink",
        fill_tags: bool = True,
        monitor: str = "none",
        dry_run: bool = True,
        timeout_s: int = 300,
    ) -> str:
        """Import a folder of downloaded files (e.g. a finished slskd download)
        into the library.

        `source_path` is the folder as Aurral sees it (e.g. under
        /store/slskd/downloads). `mode` "hardlink" keeps the source in place;
        "move" moves it. `fill_tags` lets Aurral match files to MusicBrainz by
        title and position and write tags; files it can't match are filed
        untagged and can form a separate name-based album, so check the result
        with aurral_album. Turn it off for files that already carry MusicBrainz
        IDs, or for editions MusicBrainz has no release group for.

        Replacing files the library already has: move the old files out, run
        aurral_refresh on the album, then import. Without the rescan Aurral still
        has the old file on record and skips the new one as a possible duplicate.
        Re-running an import reports already-imported files as "duplicate".

        Defaults to `dry_run`, which only checks the folder. With dry_run=false it
        imports, waits for the operation, and lists anything not imported.
        """
        if dry_run:
            check = await client.ingest_check(source_path)
            return _compact({"dry_run": True, "check": check})
        op = await client.ingest(source_path, mode, monitor, fill_tags)
        deadline = time.monotonic() + timeout_s
        while op.get("status") not in ("complete", "failed", "cancelled"):
            if time.monotonic() > deadline:
                return _compact(
                    {"operation": op["id"], "status": op.get("status"), "timeout": True}
                )
            await asyncio.sleep(2)
            op = await client.operation(op["id"])
        items = await client.operation_items(op["id"])
        not_done = [
            {"source": i.get("source"), "status": i.get("status"), "reason": i.get("reason")}
            for i in items
            if i.get("status") != "done"
        ]
        return _compact(
            {
                "operation": op["id"],
                "status": op.get("status"),
                "error": op.get("error"),
                "counts": op.get("counts"),
                "targets": sorted(
                    {posixpath.dirname(i["target"]) for i in items if i.get("target")}
                ),
                "not_imported": not_done,
            }
        )

    @server.tool(annotations=CHANGE, structured_output=False)
    async def aurral_refresh(
        album: str | None = None, artist_id: str | None = None, wait_s: int = 60
    ) -> str:
        """Rescan an album's (or an artist's) files so Aurral picks up changes made
        outside it: tags written, files moved or removed. Waits up to `wait_s` for
        the scan to finish.

        `artist_id` is the artist's library id or MusicBrainz artist id."""
        if not album and not artist_id:
            raise ToolError("pass album or artist_id")
        album_id = str((await resolve(album))["id"]) if album else None
        if artist_id and not album_id and not str(artist_id).isdigit():
            artist_id = str((await client.artist(str(artist_id).lower()))["id"])
        job = await client.refresh(album_id=album_id, artist_id=artist_id)
        status = await _wait_refresh(client, job.get("jobId"), wait_s) if wait_s > 0 else "queued"
        return _compact({"job": job.get("jobId"), "status": status})
