"""Bazarr MCP tools: which films and episodes lack subtitles, and why Bazarr
isn't looking for them."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.bazarr.client import BazarrClient

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _langs(missing: list[dict]) -> list[str]:
    out = []
    for m in missing or []:
        tag = m.get("code2") or m.get("name")
        if m.get("hi"):
            tag += ":hi"
        if m.get("forced"):
            tag += ":forced"
        out.append(tag)
    return out


def _title(x: dict) -> str:
    return f"{x.get('title')} ({x.get('year')})" if x.get("year") else x.get("title")


def register(server: MCPServer, client: BazarrClient) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def bazarr_missing(kind: str = "both", limit: int = 50) -> str:
        """Films and episodes missing subtitles in Bazarr, and items Bazarr
        isn't managing at all.

        `kind` is `movies`, `series` or `both`. For each: `wanted` (what
        Bazarr's scheduled search is still looking for, by language;
        `en:hi` = hearing-impaired, `:forced` = forced; episodes grouped by
        series with their Sonarr ids) and `no_profile`: items with no language
        profile. Those are never searched, and a manual subtitle search on
        them fails with HTTP 500, so a missing profile is the first thing to
        check when subtitles never arrive. `providers` lists subtitle
        providers that aren't `Good` (throttled or failing). Lists are cut at
        `limit` with a total.
        """
        want_movies = kind in ("movies", "both")
        want_series = kind in ("series", "both")
        calls = {"providers": client.providers()}
        if want_movies:
            calls |= {"wanted_movies": client.wanted_movies(), "movies": client.movies()}
        if want_series:
            calls |= {"wanted_eps": client.wanted_episodes(), "series": client.series()}
        got = dict(zip(calls, await asyncio.gather(*calls.values())))

        out: dict = {}
        if want_movies:
            wanted = got["wanted_movies"]
            no_profile = [m for m in got["movies"] if not m.get("profileId")]
            out["movies"] = {
                "wanted_total": len(wanted),
                "wanted": [
                    {
                        "title": m.get("title"),
                        "radarr_id": m.get("radarrId"),
                        "missing": _langs(m.get("missing_subtitles")),
                    }
                    for m in wanted[:limit]
                ],
                "no_profile_total": len(no_profile),
                "no_profile": [_title(m) for m in no_profile[:limit]] or None,
            }
        if want_series:
            by_series: dict[tuple, list] = defaultdict(list)
            for e in got["wanted_eps"]:
                key = (e.get("seriesTitle"), e.get("sonarrSeriesId"))
                langs = ",".join(_langs(e.get("missing_subtitles")))
                by_series[key].append(
                    f"{e.get('episode_number')} {e.get('episodeTitle')} [{langs}]"
                )
            no_profile = [s for s in got["series"] if not s.get("profileId")]
            groups = sorted(by_series.items(), key=lambda kv: -len(kv[1]))
            out["series"] = {
                "wanted_total": len(got["wanted_eps"]),
                "wanted": [
                    {"series": t, "sonarr_id": sid, "episodes": eps[:limit]}
                    for (t, sid), eps in groups[:limit]
                ],
                "no_profile_total": len(no_profile),
                "no_profile": [_title(s) for s in no_profile[:limit]] or None,
            }
        bad = [
            {"provider": p.get("name"), "status": p.get("status"), "retry": p.get("retry")}
            for p in got["providers"]
            if p.get("status") != "Good"
        ]
        out["providers"] = bad or "all Good"
        return _compact(out)
