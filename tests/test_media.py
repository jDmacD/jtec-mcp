"""Clients and tools for the Servarr apps, qBittorrent, Jellyfin and Bazarr,
against mock transports shaped like the live APIs."""

import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.arr import tools as arr_tools
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.bazarr import tools as bazarr_tools
from jtec_mcp.bazarr.client import BazarrClient
from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError
from jtec_mcp.jellyfin import tools as jellyfin_tools
from jtec_mcp.jellyfin.client import JellyfinClient
from jtec_mcp.qbittorrent.client import QbittorrentClient
from jtec_mcp.server import build

HASH = "1241904d0b557d7502dbb07922a352ac5bf98150"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def svc(name: str) -> ServiceConfig:
    return ServiceConfig(name, f"https://{name}.test", name.upper())


async def call(server: MCPServer, tool: str, **args):
    result = await server.call_tool(tool, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def radarr_handler(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        p = request.url.path.removeprefix("/api/v3")
        if p == "/system/status":
            return httpx.Response(
                200,
                json={"version": "7.0.3", "databaseType": "postgreSQL", "databaseVersion": "18.7"},
            )
        if p == "/health":
            return httpx.Response(
                200,
                json=[{"source": "IndexerLongTermStatusCheck", "type": "warning", "message": "IA"}],
            )
        if p == "/queue/status":
            return httpx.Response(200, json={"totalCount": 1, "unknownCount": 0, "errors": False})
        if p == "/rootfolder":
            return httpx.Response(
                200, json=[{"path": "/store/films", "freeSpace": 390e9, "accessible": True}]
            )
        if p == "/queue":
            return httpx.Response(
                200,
                json={
                    "totalRecords": 1,
                    "records": [
                        {
                            "id": 9,
                            "movie": {"title": "Resident Evil", "year": 2026},
                            "title": "Resident Evil 2026 REPACK 1080P",
                            "quality": {"quality": {"name": "WEBDL-1080p"}},
                            "size": 100,
                            "sizeleft": 36,
                            "status": "downloading",
                            "trackedDownloadState": "downloading",
                            "trackedDownloadStatus": "ok",
                            "statusMessages": [],
                            "downloadId": HASH.upper(),
                            "indexer": "Knaben",
                        }
                    ],
                },
            )
        if p == "/movie":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 447,
                        "title": "Demolition Man",
                        "year": 1993,
                        "qualityProfileId": 4,
                        "hasFile": True,
                        "monitored": False,
                        "movieFile": {
                            "relativePath": "Demolition Man.mkv",
                            "size": 9.39e9,
                            "quality": {"quality": {"name": "Bluray-1080p"}},
                            "languages": [{"name": "English"}],
                        },
                    },
                    {"id": 448, "title": "Demolition Woman", "year": 2001},
                ],
            )
        if p == "/qualityprofile":
            return httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
        if p == "/history/movie":
            assert request.url.params["movieId"] == "447"
            return httpx.Response(
                200,
                json=[
                    {
                        "date": "2026-10-22T23:54:58Z",
                        "eventType": "grabbed",
                        "sourceTitle": "Demolition Man 1993 PROPER",
                        "data": {
                            "indexer": "TPB",
                            "downloadUrl": "http://prowlarr/2/download?apikey=SECRET",
                        },
                    },
                    {
                        "date": "2026-10-23T00:08:08Z",
                        "eventType": "downloadFolderImported",
                        "sourceTitle": "Demolition Man 1993 PROPER",
                        "data": {"importedPath": "/store/films/x.mkv", "size": "9394687231"},
                    },
                    {
                        "date": "2026-11-20T17:20:03Z",
                        "eventType": "movieFileRenamed",
                        "sourceTitle": "renamed",
                    },
                ],
            )
        return httpx.Response(404, json={"message": f"no route {p}"})

    return handler


def qbit_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer qkey"
    p = request.url.path.removeprefix("/api/v2")
    if p == "/torrents/info":
        return httpx.Response(
            200,
            json=[
                {
                    "hash": HASH,
                    "name": "x",
                    "state": "stalledDL",
                    "progress": 0.64,
                    "availability": 0.64,
                    "num_seeds": 1,
                    "num_complete": 2,
                    "dlspeed": 0,
                    "eta": 8640000,
                }
            ],
        )
    if p == "/sync/torrentPeers":
        peers = {f"p{i}": {"progress": 0.64} for i in range(5)}
        peers["seed"] = {"progress": 1.0, "dl_speed": 0, "client": "Transmission 2.21"}
        return httpx.Response(200, json={"peers": peers})
    if p == "/torrents/trackers":
        return httpx.Response(200, json=[{"url": "udp://t", "status": 2, "num_seeds": 1}])
    return httpx.Response(404)


def arr_server(seen=None):
    seen = [] if seen is None else seen
    radarr = ArrClient(svc("radarr"), "rkey", transport=httpx.MockTransport(radarr_handler(seen)))
    qbit = QbittorrentClient(
        svc("qbittorrent"), "qkey", transport=httpx.MockTransport(qbit_handler)
    )
    server = MCPServer("t")
    arr_tools.register(server, {"radarr": radarr}, qbit)
    return server


@pytest.mark.anyio
async def test_arr_status_reports_health_queue_and_space():
    seen = []
    out = await call(arr_server(seen), "arr_status")
    r = out["radarr"]
    assert r["version"] == "7.0.3" and r["database"] == "postgreSQL 18.7"
    assert r["health"][0]["source"] == "IndexerLongTermStatusCheck"
    assert r["queue"]["totalCount"] == 1 and r["root_folders"][0]["free_gb"] == 390.0
    assert {q.headers["X-Api-Key"] for q in seen} == {"rkey"}


@pytest.mark.anyio
async def test_arr_queue_joins_qbittorrent_and_diagnoses_stall():
    out = await call(arr_server(), "arr_queue", problems_only=True)
    (e,) = out["queue"]
    assert e["item"] == "Resident Evil (2026)" and e["progress_pct"] == 64.0
    assert e["hash"] == HASH and e["torrent"]["state"] == "stalledDL"
    findings = " ".join(e["torrent"]["swarm"]["findings"])
    assert "stuck at 64%" in findings and "Transmission 2.21" in findings


@pytest.mark.anyio
async def test_arr_item_exact_match_with_history_and_no_secrets():
    out = await call(arr_server(), "arr_item", app="radarr", query="demolition man")
    assert out["id"] == 447 and out["quality_profile"] == "HD-1080p"
    assert out["file"]["quality"] == "Bluray-1080p" and out["file"]["size_gb"] == 9.39
    assert [h["event"] for h in out["history"]] == ["downloadFolderImported", "grabbed"]
    assert out["hidden_renames"] == 1
    assert out["history"][0]["size"] == 9.39
    assert "SECRET" not in json.dumps(out)
    full = await call(
        arr_server(), "arr_item", app="radarr", query="demolition man", include_renames=True
    )
    assert full["history"][0]["event"] == "movieFileRenamed" and "hidden_renames" not in full


@pytest.mark.anyio
async def test_arr_item_lists_partial_matches_and_rejects_unknown_app():
    out = await call(arr_server(), "arr_item", app="radarr", query="demolition")
    assert [m["id"] for m in out["matches"]] == [447, 448]
    with pytest.raises(ToolError, match="unknown app 'sonarr'"):
        await arr_server().call_tool("arr_item", {"app": "sonarr", "query": "x"})


def jellyfin_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == 'MediaBrowser Token="jkey"'
    p = request.url.path
    remux = {
        "Bitrate": 66_913_544,
        "Size": 82e9,
        "MediaStreams": [
            {"Index": 0, "Type": "Video", "Codec": "hevc", "VideoRangeType": "HDR10"},
            {"Index": 1, "Type": "Audio", "Codec": "truehd", "IsDefault": True, "Language": "eng"},
        ],
    }
    if p == "/Sessions":
        return httpx.Response(
            200,
            json=[
                {
                    "UserName": "cast",
                    "Client": "Wholphin",
                    "DeviceName": "Cast",
                    "NowPlayingItem": {
                        "Type": "Episode",
                        "SeriesName": "Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 2,
                        "Name": "Two",
                        "RunTimeTicks": 36_000_000_000,
                        "MediaStreams": remux["MediaStreams"],
                    },
                    "PlayState": {
                        "PlayMethod": "Transcode",
                        "PositionTicks": 600_000_000,
                        "AudioStreamIndex": 1,
                        "SubtitleStreamIndex": -1,
                    },
                    "TranscodingInfo": {
                        "TranscodeReasons": ["AudioCodecNotSupported"],
                        "IsVideoDirect": True,
                        "AudioCodec": "ac3",
                        "Bitrate": 41_600_000,
                    },
                },
                {"UserName": "james", "Client": "Web"},
            ],
        )
    if p == "/Items" and request.url.params.get("searchTerm"):
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "m1",
                        "Type": "Movie",
                        "Name": "Blade Runner 2049",
                        "ProductionYear": 2017,
                        "MediaSources": [remux],
                    },
                    {"Id": "s1", "Type": "Series", "Name": "Blade Runner 2049 Shorts"},
                ]
            },
        )
    if p == "/Shows/s1/Episodes":
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Type": "Episode",
                        "SeriesName": "S",
                        "ParentIndexNumber": 1,
                        "IndexNumber": n,
                        "Name": f"E{n}",
                        "MediaSources": [remux],
                    }
                    for n in (1, 2)
                ]
            },
        )
    if p == "/Items":
        return httpx.Response(
            200,
            json={"Items": [{"Id": "s1", "Type": "Series", "Name": "Blade Runner 2049 Shorts"}]},
        )
    return httpx.Response(404)


def jellyfin_server():
    c = JellyfinClient(svc("jellyfin"), "jkey", transport=httpx.MockTransport(jellyfin_handler))
    server = MCPServer("t")
    jellyfin_tools.register(server, c)
    return server


@pytest.mark.anyio
async def test_jellyfin_sessions_shows_playing_only():
    out = await call(jellyfin_server(), "jellyfin_sessions")
    (s,) = out["playing"]
    assert out["idle"] == 1
    assert s["title"] == "Show S01E02 Two" and s["position"] == "0:01:00 / 1:00:00"
    assert s["transcode"]["video"] == "copy" and s["transcode"]["audio"] == "ac3"
    assert s["audio"]["codec"] == "truehd" and s["subtitle"] is None


@pytest.mark.anyio
async def test_playback_check_exact_title_then_series_season():
    out = await call(jellyfin_server(), "jellyfin_playback_check", query="Blade Runner 2049")
    v = out["versions"][0]
    assert out["title"] == "Blade Runner 2049 (2017)" and v["verdict"] == "transcode"
    season = await call(jellyfin_server(), "jellyfin_playback_check", item_id="s1", episode="s01")
    assert [e["episode"] for e in season["episodes"]] == ["S S01E01 E1", "S S01E02 E2"]
    assert season["episodes"][0]["why"] == [
        "ContainerBitrateExceedsLimit",
        "AudioCodecNotSupported",
    ]
    with pytest.raises(ToolError, match="is a series"):
        await jellyfin_server().call_tool("jellyfin_playback_check", {"item_id": "s1"})


def bazarr_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-API-KEY"] == "bkey"
    p = request.url.path.removeprefix("/api")
    if p != "/providers":
        assert request.url.params["length"] == "-1"
    data = {
        "/movies/wanted": [
            {
                "title": "The Lure",
                "radarrId": 3,
                "missing_subtitles": [{"code2": "en", "hi": False}],
            }
        ],
        "/movies": [
            {"title": "A", "year": "2020", "profileId": 1},
            {"title": "B", "year": "2021", "profileId": None},
        ],
        "/episodes/wanted": [
            {
                "seriesTitle": "BSG",
                "sonarrSeriesId": 43,
                "episode_number": "0x19",
                "episodeTitle": "Face",
                "missing_subtitles": [{"code2": "en", "hi": True}],
            },
            {
                "seriesTitle": "BSG",
                "sonarrSeriesId": 43,
                "episode_number": "0x20",
                "episodeTitle": "Face 2",
                "missing_subtitles": [{"code2": "en"}],
            },
        ],
        "/series": [{"title": "S", "profileId": 1}],
        "/providers": [
            {"name": "opensubtitlescom", "status": "DownloadLimitExceeded", "retry": "6h"},
            {"name": "subdl", "status": "Good"},
        ],
    }
    return httpx.Response(200, json={"data": data[p], "total": len(data[p])})


@pytest.mark.anyio
async def test_bazarr_missing_groups_episodes_and_flags_profiles():
    c = BazarrClient(svc("bazarr"), "bkey", transport=httpx.MockTransport(bazarr_handler))
    server = MCPServer("t")
    bazarr_tools.register(server, c)
    out = await call(server, "bazarr_missing")
    assert out["movies"]["wanted"][0]["missing"] == ["en"]
    assert out["movies"]["no_profile"] == ["B (2021)"]
    (bsg,) = out["series"]["wanted"]
    assert bsg["sonarr_id"] == 43 and bsg["episodes"][0] == "0x19 Face [en:hi]"
    assert out["series"]["no_profile_total"] == 0
    assert out["providers"] == [
        {"provider": "opensubtitlescom", "status": "DownloadLimitExceeded", "retry": "6h"}
    ]


@pytest.mark.anyio
async def test_registration_follows_configured_services(monkeypatch):
    for name in ("RADARR", "SONARR", "PROWLARR", "QBITTORRENT", "JELLYFIN", "BAZARR"):
        monkeypatch.setenv(name, "k")
    arr = {
        "arr_status",
        "arr_queue",
        "arr_item",
        "arr_queue_remove",
        "arr_search",
        "arr_set_monitored",
    }
    names = {t.name for t in await build({"radarr": svc("radarr")}).list_tools()}
    assert names == arr
    with_prowlarr = {"radarr": svc("radarr"), "prowlarr": svc("prowlarr")}
    names = {t.name for t in await build(with_prowlarr).list_tools()}
    assert names == arr | {"prowlarr_indexers", "prowlarr_test", "prowlarr_set_indexer"}
    services = {n: svc(n) for n in ("radarr", "sonarr", "qbittorrent", "jellyfin", "bazarr")}
    names = {t.name for t in await build(services).list_tools()}
    assert names == arr | {
        "qbittorrent_torrent",
        "jellyfin_sessions",
        "jellyfin_playback_check",
        "bazarr_missing",
        "bazarr_subtitle_search",
        "bazarr_download_subtitle",
    }
    monkeypatch.delenv("RADARR")
    monkeypatch.delenv("SONARR")
    assert "arr_status" not in {t.name for t in await build(services).list_tools()}
