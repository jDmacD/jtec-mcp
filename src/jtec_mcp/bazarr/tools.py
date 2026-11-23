"""Bazarr MCP tools: which films and episodes lack subtitles, and why Bazarr
isn't looking for them."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.bazarr.client import BazarrClient
from jtec_mcp.errors import ToolError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SEARCH = ToolAnnotations(read_only_hint=True, open_world_hint=True)
DOWNLOAD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
KINDS = ("movie", "episode")


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

    # Last search per item: download picks a candidate by its number, so the
    # long opaque `subtitle` token never goes through the agent.
    found: dict[tuple[str, int], list[dict]] = {}

    def kind_of(kind: str) -> str:
        k = kind.lower().rstrip("s")
        if k not in KINDS:
            raise ToolError("kind is `movie` (Radarr id) or `episode` (Sonarr episode id)")
        return k

    @server.tool(annotations=SEARCH, structured_output=False)
    async def bazarr_subtitle_search(kind: str, item_id: int, limit: int = 10) -> str:
        """Search Bazarr's subtitle providers for one film or episode.

        `kind` is `movie` (with its Radarr id, as bazarr_missing shows) or
        `episode` (with its Sonarr episode id). Candidates come numbered, best
        score first, with provider, language, HI/forced flags, the score, the
        release names the subtitle was made for, and what did and didn't match
        the file. Check the release names: the top score can still be for a
        different cut or a different show. Pass a number to
        bazarr_download_subtitle. The item needs a language profile (Bazarr
        answers 500 otherwise).
        """
        k = kind_of(kind)
        try:
            results = await client.candidates(k, item_id)
        except ToolError as e:
            if " 500 " in str(e):
                raise ToolError(
                    f"{e}. Bazarr fails like this when the item has no language "
                    "profile; check bazarr_missing's no_profile list"
                ) from e
            raise
        results.sort(key=lambda r: r.get("score") or 0, reverse=True)
        found[(k, item_id)] = results
        return _compact(
            {
                "total": len(results),
                "candidates": [
                    {
                        "n": n,
                        "provider": r.get("provider"),
                        "language": r.get("language"),
                        "hi": r.get("hearing_impaired") == "True",
                        "forced": r.get("forced") == "True",
                        "score": r.get("score"),
                        "releases": (r.get("release_info") or [])[:3],
                        "matched": r.get("matches"),
                        "unmatched": r.get("dont_matches"),
                        "uploader": r.get("uploader"),
                    }
                    for n, r in enumerate(results[:limit], 1)
                ],
            }
        )

    @server.tool(annotations=DOWNLOAD, structured_output=False)
    async def bazarr_download_subtitle(kind: str, item_id: int, number: int) -> str:
        """Download candidate `number` from the last bazarr_subtitle_search for
        this item. Bazarr saves it next to the media file as an external
        subtitle (replacing one in the same language and flags). It accepts the
        request at once and writes the file a few seconds later, so check
        bazarr_missing or the file after a short wait.
        """
        k = kind_of(kind)
        results = found.get((k, item_id))
        if not results:
            raise ToolError("search first: bazarr_subtitle_search for this item")
        if not 1 <= number <= len(results):
            raise ToolError(f"number must be 1..{len(results)}")
        cand = results[number - 1]
        series_id = None
        if k == "episode":
            ep = await client.episode(item_id)
            if not ep:
                raise ToolError(f"Bazarr has no episode {item_id}")
            series_id = ep.get("sonarrSeriesId")
        await client.download(k, item_id, series_id, cand)
        return _compact(
            {
                "requested": {
                    "provider": cand.get("provider"),
                    "language": cand.get("language"),
                    "releases": (cand.get("release_info") or [])[:3],
                    "score": cand.get("score"),
                },
                "note": "Bazarr writes the file a few seconds after accepting",
            }
        )
