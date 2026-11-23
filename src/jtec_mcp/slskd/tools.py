"""slskd MCP tools: album search, transfer status and chats."""

from __future__ import annotations

import json
from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.slskd.client import SlskdClient
from jtec_mcp.slskd.ranking import Folder, folders_from_responses, rank

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# A search creates a search record on slskd and queries the Soulseek network,
# but changes nothing else.
QUEUE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
CLEAR = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
SEND = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
ACK = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
FINISHED = "Completed"  # slskd states: "Completed, Succeeded", "Completed, Errored", ...
SEARCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def _compact(obj) -> str:
    """Compact JSON: the SDK would otherwise pretty-print results (and split lists)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _failure(item) -> dict:
    """slskd's enqueue response lists failures; tolerate a string or an object."""
    if isinstance(item, dict):
        name = item.get("filename") or item.get("file") or ""
        return {"file": name.rpartition("\\")[2], "error": item.get("error") or item.get("message")}
    return {"error": str(item)}


def register(server: MCPServer, client: SlskdClient) -> None:
    # Ranked folders from recent searches, so a later download tool can refer
    # to "folder n of search X" without the agent copying file lists around.
    searches: dict[str, list[Folder]] = {}

    @server.tool(annotations=SEARCH, structured_output=False)
    async def slskd_search_album(
        query: str,
        track_titles: list[str] | None = None,
        min_tracks: int = 1,
        limit: int = 15,
        timeout_s: int = 90,
    ) -> str:
        """Search Soulseek for a whole album and rank complete folders.

        Groups results by peer folder (one peer's copy of one edition) and ranks
        them: most expected tracks first (when `track_titles` is given, by how many
        titles a folder's filenames contain; otherwise by audio file count), then
        lossless before lossy, then free upload slot, short queue, fast upload.

        Use `track_titles` (the album's track list) for reliable completeness;
        `min_tracks` drops folders with fewer audio files. Waits up to `timeout_s`
        for the search to finish. Returns the search id and the top `limit`
        folders, each numbered `n`.
        """
        search_id, responses, completed = await client.search(query, timeout_s=timeout_s)
        ranked = rank(folders_from_responses(responses, track_titles), min_tracks=min_tracks)
        searches[search_id] = ranked
        return _compact(
            {
                "search_id": search_id,
                "completed": completed,
                "peers_responded": len(responses),
                "folders_found": len(ranked),
                "expected_tracks": len(track_titles) if track_titles else None,
                "folders": [f.summary(i + 1) for i, f in enumerate(ranked[:limit])],
            }
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def slskd_downloads(username: str | None = None, show_completed: bool = False) -> str:
        """Summarise download transfers, per peer and folder.

        Gives counts by state for each folder, and lists files that are not
        "Completed, Succeeded" (or every file with `show_completed`). Limit to one
        peer with `username`.
        """
        out = []
        for user in await client.downloads(username):
            for d in user.get("directories", []):
                files = d.get("files", [])
                states = Counter(f.get("state", "?") for f in files)
                listed = [
                    {
                        "file": f["filename"].rpartition("\\")[2],
                        "state": f.get("state"),
                        "percent": round(f.get("percentComplete") or 0, 1),
                        "id": f.get("id"),
                    }
                    for f in files
                    if show_completed or f.get("state") != "Completed, Succeeded"
                ]
                out.append(
                    {
                        "user": user.get("username"),
                        "folder": d.get("directory"),
                        "states": dict(states),
                        "bytes_remaining": sum(f.get("bytesRemaining") or 0 for f in files),
                        "files": listed,
                    }
                )
        return _compact(out)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def slskd_conversations(unread_only: bool = True, last_messages: int = 3) -> str:
        """List Soulseek private chats, with each one's most recent messages.

        Peers often run anti-bot plugins that ask a question (a word to type back)
        before serving files; those challenges arrive here. By default only chats
        with unacknowledged messages are shown.
        """
        out = []
        for c in await client.conversations():
            if unread_only and not c.get("hasUnAcknowledgedMessages"):
                continue
            full = await client.conversation(c["username"])
            msgs = sorted(full.get("messages") or [], key=lambda m: m.get("timestamp", ""))
            out.append(
                {
                    "user": c["username"],
                    "unread": c.get("unAcknowledgedMessageCount", 0),
                    "messages": [
                        {
                            "at": m.get("timestamp"),
                            "dir": m.get("direction"),
                            "text": m.get("message"),
                        }
                        # msgs[-0:] would be every message
                        for m in (msgs[-last_messages:] if last_messages > 0 else [])
                    ],
                }
            )
        return _compact(out)

    @server.tool(annotations=QUEUE, structured_output=False)
    async def slskd_download_folder(
        search_id: str,
        n: int | None = None,
        username: str | None = None,
        folder: str | None = None,
        audio_only: bool = False,
    ) -> str:
        """Queue every file of one peer folder from an album search.

        Pick the folder by its number `n` from slskd_search_album's results, or by
        `username` and `folder` (exact path as listed) if this server restarted
        since the search. Downloads the whole folder (cover art too) unless
        `audio_only`. Files are requested with the exact names and sizes the peer
        reported, which slskd requires.
        """
        if username and folder:
            responses = await client.search_responses(search_id)
            matches = [
                f
                for f in folders_from_responses(responses)
                if f.username == username and f.path == folder
            ]
            if not matches:
                raise ToolError(f"no folder {folder!r} from {username!r} in search {search_id}")
            chosen = matches[0]
        elif n is not None:
            ranked = searches.get(search_id)
            if ranked is None:
                raise ToolError(
                    f"search {search_id} is not cached here; pass username and folder instead"
                )
            if not 1 <= n <= len(ranked):
                raise ToolError(f"n must be between 1 and {len(ranked)}")
            chosen = ranked[n - 1]
        else:
            raise ToolError("pass n, or username and folder")

        files = chosen.audio if audio_only else chosen.files
        result = await client.enqueue(chosen.username, files)
        return _compact(
            {
                "user": chosen.username,
                "folder": chosen.path,
                "requested": len(files),
                "enqueued": len(result.get("enqueued", [])),
                "failed": [_failure(f) for f in result.get("failed", [])],
                "size_mb": round(sum(f.get("size", 0) for f in files) / 1e6, 1),
            }
        )

    @server.tool(annotations=CLEAR, structured_output=False)
    async def slskd_clear_downloads(
        username: str | None = None,
        failed_only: bool = False,
        cancel_unfinished: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Remove download entries from slskd's transfer list (files on disk stay).

        By default removes finished entries (any "Completed, ..." state) for every
        peer, or only `username`'s. `failed_only` keeps successful ones.
        `cancel_unfinished` also cancels and removes queued or running transfers,
        e.g. a peer whose copy turned out wrong. Defaults to `dry_run`: lists what
        would go; call again with dry_run=false to do it.
        """
        selected = []
        for user in await client.downloads(username):
            for d in user.get("directories", []):
                for f in d.get("files", []):
                    state = f.get("state", "")
                    finished = state.startswith(FINISHED)
                    if failed_only and state == "Completed, Succeeded":
                        continue
                    if finished or cancel_unfinished:
                        selected.append((user["username"], f))
        if not dry_run:
            for user, f in selected:
                await client.remove_download(user, f["id"])
        states = Counter(f.get("state", "?") for _, f in selected)
        return _compact(
            {
                "dry_run": dry_run,
                "removed" if not dry_run else "would_remove": len(selected),
                "by_state": dict(states),
                "users": sorted({u for u, _ in selected}),
            }
        )

    @server.tool(annotations=SEND, structured_output=False)
    async def slskd_reply(username: str, message: str) -> str:
        """Send a private message to a Soulseek user.

        Typically the answer to a peer's anti-bot challenge seen with
        slskd_conversations (e.g. the word it asks you to type). Also marks their
        messages as read.
        """
        await client.send_message(username, message)
        await client.acknowledge(username)
        return _compact({"sent_to": username, "message": message})

    @server.tool(annotations=ACK, structured_output=False)
    async def slskd_acknowledge(username: str) -> str:
        """Mark all of a user's messages as read, without replying."""
        await client.acknowledge(username)
        return _compact({"acknowledged": username})
