"""Write tools: dry runs change nothing; real runs send the right requests."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.arr import tools as arr_tools
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.bazarr import tools as bazarr_tools
from jtec_mcp.bazarr.client import BazarrClient
from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def svc(name):
    return ServiceConfig(name, f"https://{name}.test", name.upper())


async def call(server, tool, **args):
    result = await server.call_tool(tool, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


class FakeArr:
    def __init__(self, version="v3"):
        self.v = version
        self.writes: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path.removeprefix(f"/api/{self.v}"), request.method
        if m != "GET":
            self.writes.append(request)
        if p == "/queue":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": 9,
                            "title": "Crookers",
                            "status": "queued",
                            "trackedDownloadState": "downloading",
                            "trackedDownloadStatus": "ok",
                            "downloadId": "AB" * 20,
                        }
                    ]
                },
            )
        if p == "/queue/9" and m == "DELETE":
            return httpx.Response(200)
        if p == "/command" and m == "POST":
            return httpx.Response(
                201, json={"id": 5, "name": json.loads(request.content)["name"], "status": "queued"}
            )
        if p == "/command/5":
            return httpx.Response(200, json={"id": 5, "name": "x", "status": "completed"})
        if p == "/episode":
            return httpx.Response(200, json=[{"id": 77, "seasonNumber": 2, "episodeNumber": 3}])
        if p == "/artist":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "artistName": "A", "monitored": False},
                    {"id": 9, "artistName": "Crookers", "monitored": True},
                ],
            )
        if p == "/album":
            return httpx.Response(
                200,
                json=[
                    {"id": 70, "artistId": 9, "monitored": True},
                    {"id": 71, "artistId": 1, "monitored": True},
                ],
            )
        if p in ("/artist/editor", "/album/monitor"):
            return httpx.Response(202, json=[])
        if p == "/indexer/3" and m == "PUT":
            return httpx.Response(202, json=json.loads(request.content))
        if p == "/indexer/3":
            return httpx.Response(200, json={"id": 3, "name": "IA", "enable": False})
        return httpx.Response(404, json={"message": f"no route {m} {p}"})


def server_for(name, fake):
    c = ArrClient(svc(name), "k", transport=httpx.MockTransport(fake))
    s = MCPServer("t")
    arr_tools.register(s, {name: c})
    return s


@pytest.mark.anyio
async def test_queue_remove_dry_run_then_real():
    fake = FakeArr("v1")
    s = server_for("lidarr", fake)
    out = await call(s, "arr_queue_remove", app="lidarr", queue_id=9)
    assert out["dry_run"] and out["would_remove"]["release"] == "Crookers" and not fake.writes
    await call(s, "arr_queue_remove", app="lidarr", queue_id=9, dry_run=False, search=True)
    (req,) = fake.writes
    assert req.method == "DELETE" and dict(req.url.params) == {
        "removeFromClient": "true",
        "blocklist": "true",
        "skipRedownload": "false",
    }
    with pytest.raises(ToolError, match="no queue item 1"):
        await s.call_tool("arr_queue_remove", {"app": "lidarr", "queue_id": 1})


@pytest.mark.anyio
async def test_search_item_and_sonarr_episode():
    fake = FakeArr()
    out = await call(server_for("radarr", fake), "arr_search", app="radarr", item_id=447, wait_s=0)
    assert json.loads(fake.writes[0].content) == {"name": "MoviesSearch", "movieIds": [447]}
    assert out["command_id"] == 5
    fake = FakeArr()
    s = server_for("sonarr", fake)
    await call(s, "arr_search", app="sonarr", item_id=72, episode="s02e03", wait_s=0)
    await call(s, "arr_search", app="sonarr", item_id=72, episode="S02", wait_s=0)
    bodies = [json.loads(r.content) for r in fake.writes]
    assert bodies == [
        {"name": "EpisodeSearch", "episodeIds": [77]},
        {"name": "SeasonSearch", "seriesId": 72, "seasonNumber": 2},
    ]


@pytest.mark.anyio
async def test_lidarr_unmonitor_all_includes_albums():
    fake = FakeArr("v1")
    s = server_for("lidarr", fake)
    out = await call(s, "arr_set_monitored", app="lidarr", monitored=False, all_items=True)
    assert out["dry_run"] and out["changing"] == ["Crookers (9)"] and not fake.writes
    assert out["albums_unmonitoring"] == 2
    await call(s, "arr_set_monitored", app="lidarr", monitored=False, all_items=True, dry_run=False)
    editor, albums = (json.loads(r.content) for r in fake.writes)
    assert editor == {"artistIds": [1, 9], "monitored": False, "monitorNewItems": "none"}
    assert albums == {"albumIds": [70, 71], "monitored": False}
    with pytest.raises(ToolError, match="usable here"):
        await server_for("chaptarr", FakeArr("v1")).call_tool(
            "arr_set_monitored", {"app": "chaptarr", "monitored": False, "all_items": True}
        )


@pytest.mark.anyio
async def test_prowlarr_enable_indexer():
    fake = FakeArr("v1")
    s = server_for("prowlarr", fake)
    out = await call(s, "prowlarr_set_indexer", indexer_id=3, enabled=True, force=True)
    assert out == {"id": 3, "name": "IA", "enabled": True}
    (put,) = fake.writes
    assert put.url.params["forceSave"] == "true" and json.loads(put.content)["enable"] is True


def bazarr_handler(sent):
    def handler(request):
        p = request.url.path.removeprefix("/api")
        if request.method == "POST":
            sent.append(request)
            return httpx.Response(204)
        if p == "/providers/episodes":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "provider": "subdl",
                            "score": 80,
                            "subtitle": "TOKEN-B",
                            "language": "en",
                            "hearing_impaired": "False",
                            "forced": "False",
                            "original_format": "False",
                            "release_info": ["Show.S01E01"],
                        },
                        {
                            "provider": "opensubtitlescom",
                            "score": 93,
                            "subtitle": "TOKEN-A",
                            "language": "en",
                            "hearing_impaired": "True",
                            "forced": "False",
                            "original_format": "False",
                            "release_info": ["Show.S01E01.720p"],
                        },
                    ]
                },
            )
        if p == "/providers/movies":
            return httpx.Response(500, json={"message": "TypeError"})
        if p == "/episodes":
            return httpx.Response(200, json={"data": [{"sonarrSeriesId": 43}]})
        return httpx.Response(404)

    return handler


@pytest.mark.anyio
async def test_bazarr_search_then_download_by_number():
    sent = []
    c = BazarrClient(svc("bazarr"), "k", transport=httpx.MockTransport(bazarr_handler(sent)))
    s = MCPServer("t")
    bazarr_tools.register(s, c)
    with pytest.raises(ToolError, match="search first"):
        await s.call_tool(
            "bazarr_download_subtitle", {"kind": "episode", "item_id": 2185, "number": 1}
        )
    out = await call(s, "bazarr_subtitle_search", kind="episode", item_id=2185)
    assert [x["provider"] for x in out["candidates"]] == ["opensubtitlescom", "subdl"]
    assert out["candidates"][0]["hi"] is True and "TOKEN" not in json.dumps(out)
    await call(s, "bazarr_download_subtitle", kind="episodes", item_id=2185, number=1)
    (post,) = sent
    assert dict(post.url.params) == {"seriesid": "43", "episodeid": "2185"}
    form = {k: v[0] for k, v in parse_qs(post.content.decode()).items()}
    assert (
        form["subtitle"] == "TOKEN-A"
        and form["hi"] == "True"
        and form["provider"] == "opensubtitlescom"
    )
    with pytest.raises(ToolError, match="no language profile"):
        await s.call_tool("bazarr_subtitle_search", {"kind": "movie", "item_id": 29})
