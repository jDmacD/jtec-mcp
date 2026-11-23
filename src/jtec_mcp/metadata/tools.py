"""Album editions and track lists, to drive slskd searches and Aurral editions."""

from __future__ import annotations

import json
from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.metadata.client import MetadataClient

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _tracks(release: dict) -> list[dict]:
    ts = sorted(
        release.get("Tracks") or [],
        key=lambda t: (t.get("MediumNumber") or 1, t.get("TrackPosition") or 0),
    )
    return [
        {
            "pos": f"{t.get('MediumNumber') or 1}.{t.get('TrackPosition')}",
            "title": t.get("TrackName"),
            "seconds": round(t["DurationMs"] / 1000) if t.get("DurationMs") else None,
        }
        for t in ts
    ]


def _standard(releases: list[dict]) -> dict | None:
    """The edition most releases share: commonest track count, CD preferred."""
    if not releases:
        return None
    common = Counter(r.get("TrackCount") for r in releases).most_common(1)[0][0]
    pool = [r for r in releases if r.get("TrackCount") == common]
    cds = [r for r in pool if any(m.get("Format") == "CD" for m in r.get("Media") or [])]
    return (cds or pool)[0]


def register(server: MCPServer, client: MetadataClient) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def metadata_album(
        release_group: str | None = None,
        query: str | None = None,
        release: str | None = None,
    ) -> str:
        """MusicBrainz album data: editions (releases) and a track list.

        Give `release_group` (an album's MBID, e.g. from aurral_search) for its
        editions: id, country, formats, label, date and track count. The track
        list (titles and durations) is for `release` if given, otherwise for the
        standard edition (the commonest track count, CD preferred). Feed the
        titles to slskd_search_album as `track_titles`, and match a downloaded
        copy's track order and durations to an edition for aurral_set_edition.

        Or give `query` ("artist album") to find release groups first.
        """
        if query and not release_group:
            hits = await client.search_albums(query)
            return _compact(
                [
                    {
                        "release_group": h.get("id"),
                        "title": h.get("title"),
                        "type": h.get("type"),
                        "secondary": h.get("secondarytypes") or None,
                        "date": h.get("releasedate"),
                        "artist_mbid": h.get("artistid"),
                    }
                    for h in hits[:10]
                ]
            )
        if not release_group:
            raise ToolError("pass release_group, or query to find one")
        album = await client.album(release_group)
        releases = album.get("Releases") or []
        chosen = (
            next((r for r in releases if r.get("Id") == release), None)
            if release
            else _standard(releases)
        )
        if release and chosen is None:
            raise ToolError(f"release {release} is not an edition of {release_group}")
        return _compact(
            {
                "release_group": album.get("id") or release_group,
                "title": album.get("title"),
                "type": album.get("type"),
                "date": album.get("releasedate"),
                "editions": [
                    {
                        "id": r.get("Id"),
                        "title": r.get("Title"),
                        "country": r.get("Country") or None,
                        "formats": [m.get("Format") for m in r.get("Media") or []],
                        "label": r.get("Label") or None,
                        "date": r.get("ReleaseDate"),
                        "tracks": r.get("TrackCount"),
                    }
                    for r in releases
                ],
                "track_list_of": chosen.get("Id") if chosen else None,
                "track_list": _tracks(chosen) if chosen else [],
            }
        )
