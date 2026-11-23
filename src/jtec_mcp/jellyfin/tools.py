"""Jellyfin MCP tools: what's playing and how, and whether an item will direct
play on a given client."""

from __future__ import annotations

import json
import re

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.jellyfin import playback
from jtec_mcp.jellyfin.client import JellyfinClient

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
TICKS = 10_000_000
EPISODE = re.compile(r"^s(\d+)(?:e(\d+))?$", re.IGNORECASE)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _hms(ticks) -> str | None:
    if ticks is None:
        return None
    s = int(ticks) // TICKS
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _title(item: dict) -> str:
    if item.get("Type") == "Episode":
        return (
            f"{item.get('SeriesName')} S{item.get('ParentIndexNumber') or 0:02d}"
            f"E{item.get('IndexNumber') or 0:02d} {item.get('Name')}"
        )
    year = item.get("ProductionYear")
    return f"{item.get('Name')} ({year})" if year else item.get("Name")


def _session(s: dict) -> dict:
    item = s["NowPlayingItem"]
    state = s.get("PlayState") or {}
    streams = item.get("MediaStreams") or []

    def pick(index):
        if index is None or index < 0:
            return None
        found = next((x for x in streams if x.get("Index") == index), None)
        return playback.describe_stream(found) if found else None

    video = next((x for x in streams if x.get("Type") == "Video"), None)
    out = {
        "user": s.get("UserName"),
        "client": f"{s.get('Client')} {s.get('ApplicationVersion') or ''}".strip(),
        "device": s.get("DeviceName"),
        "title": _title(item),
        "item_id": item.get("Id"),
        "paused": state.get("IsPaused"),
        "position": f"{_hms(state.get('PositionTicks'))} / {_hms(item.get('RunTimeTicks'))}",
        "play_method": state.get("PlayMethod"),
        "source": {
            "container": item.get("Container"),
            "video": playback.describe_stream(video) if video else None,
        },
        "audio": pick(state.get("AudioStreamIndex")),
        "subtitle": pick(state.get("SubtitleStreamIndex")),
    }
    if tx := s.get("TranscodingInfo"):
        out["transcode"] = {
            "reasons": tx.get("TranscodeReasons"),
            "video": "copy" if tx.get("IsVideoDirect") else tx.get("VideoCodec"),
            "audio": "copy" if tx.get("IsAudioDirect") else tx.get("AudioCodec"),
            "audio_channels": tx.get("AudioChannels"),
            "mbps": round((tx.get("Bitrate") or 0) / 1e6, 1),
            "hw": tx.get("HardwareAccelerationType"),
            "res": f"{tx.get('Width')}x{tx.get('Height')}" if tx.get("Width") else None,
            "done_pct": round(tx["CompletionPercentage"], 1)
            if tx.get("CompletionPercentage") is not None
            else None,
        }
    return out


def register(server: MCPServer, client: JellyfinClient) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def jellyfin_sessions(active_within_min: int = 15) -> str:
        """What is playing on Jellyfin right now, and how.

        One entry per session with something playing (paused counts): user,
        client and version, device, title, position, PlayMethod (DirectPlay,
        DirectStream, Transcode), the source's video, the selected audio and
        subtitle streams, and for transcodes Jellyfin's TranscodeReasons, the
        output codecs (`copy` = passed through), bitrate and hardware
        acceleration. Jellyfin doesn't see client buffering, so a stall on the
        TV needs the device's own logs. `idle` counts sessions seen within
        `active_within_min` with nothing playing.
        """
        sessions = await client.sessions(active_within_min * 60)
        playing = [_session(s) for s in sessions if s.get("NowPlayingItem")]
        return _compact({"playing": playing, "idle": len(sessions) - len(playing)})

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def jellyfin_playback_check(
        query: str = "", item_id: str = "", episode: str = "", client_profile: str = "tv"
    ) -> str:
        """Predict whether a film or episode will direct play on a client, and why not.

        Find the item by `query` (title search) or `item_id`. For a series, give
        `episode` as `S01E02`, or `S01` to check a whole season (one line per
        episode). The check applies the client profile's limits (`tv`: 40 Mb/s
        bitrate cap, HEVC/H.264/VP9, HDR10/HLG; Dolby Vision plays from its base
        layer, so only profile 5-style DV with no fallback transcodes;
        AC3/E-AC3 passthrough, TrueHD transcodes) to the default audio and
        subtitle streams, as a user with subtitle mode "Default" gets them.
        It reports the `verdict`; each `transcode` reason (named as Jellyfin
        names them, each marked `observed` or `assumed`); `risks`, e.g. an
        embedded text subtitle whose first use makes Jellyfin read the whole
        file; and `tips`, e.g. an AC3/E-AC3 track that would avoid the audio
        transcode. The source's audio tracks are listed, and its subtitles in
        the audio's language (plus forced, default and external ones); the
        rest are counted in `other_subtitles`.
        """
        profile = playback.PROFILES.get(client_profile)
        if profile is None:
            raise ToolError(
                f"unknown profile {client_profile!r}; known: {', '.join(playback.PROFILES)}"
            )
        if item_id:
            found = await client.item(item_id)
            items = [found] if found else []
        elif query:
            items = await client.search(query)
        else:
            raise ToolError("give a query or an item_id")
        if not items:
            raise ToolError(f"Jellyfin has nothing matching {query or item_id!r}")

        exact = [i for i in items if (i.get("Name") or "").lower() == query.lower()]
        items = exact or items
        if len(items) > 1:
            return _compact(
                {
                    "matches": [
                        {"id": i.get("Id"), "type": i.get("Type"), "title": _title(i)}
                        for i in items
                    ]
                }
            )
        item = items[0]

        if item.get("Type") == "Series":
            m = EPISODE.match(episode.strip())
            if not m:
                raise ToolError(
                    f"{item.get('Name')} is a series: give episode='S01E02', or 'S01' for a season"
                )
            season, number = int(m.group(1)), m.group(2)
            eps = await client.episodes(item["Id"], season)
            if number is not None:
                eps = [e for e in eps if e.get("IndexNumber") == int(number)]
                if not eps:
                    raise ToolError(f"{item.get('Name')} has no S{season:02d}E{int(number):02d}")
            if len(eps) > 1:
                lines = []
                for e in eps:
                    src = (e.get("MediaSources") or [None])[0]
                    if not src:
                        lines.append({"episode": _title(e), "verdict": "no file"})
                        continue
                    r = playback.check(src, profile)
                    lines.append(
                        {
                            "episode": _title(e),
                            "verdict": r["verdict"],
                            "why": [t["reason"] for t in r["transcode"] or []] or None,
                            "risks": len(r["risks"] or []) or None,
                        }
                    )
                return _compact({"profile": profile.name, "episodes": lines})
            item = eps[0]

        sources = item.get("MediaSources") or []
        if not sources:
            raise ToolError(f"{_title(item)} has no media file")
        return _compact(
            {
                "title": _title(item),
                "item_id": item.get("Id"),
                "profile": profile.name,
                "notes": profile.notes,
                "versions": [
                    {"path": s.get("Path"), **playback.check(s, profile)} for s in sources
                ],
            }
        )
