"""slskd MCP tools: album search, transfer status and chats."""

from __future__ import annotations

import json
from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.slskd.client import SlskdClient
from jtec_mcp.slskd.ranking import Folder, folders_from_responses, rank

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# A search creates a search record on slskd and queries the Soulseek network,
# but changes nothing else.
SEARCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def _compact(obj) -> str:
    """Compact JSON: the SDK would otherwise pretty-print results (and split lists)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


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
                        for m in msgs[-last_messages:]
                    ],
                }
            )
        return _compact(out)
