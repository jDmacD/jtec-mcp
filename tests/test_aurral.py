import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.aurral import tools as aurral_tools
from jtec_mcp.aurral.client import AurralClient, album_ref
from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError

SVC = ServiceConfig("aurral", "https://aurral.test", "AURRAL")
RG = "1346c819-d57b-4c38-aa55-1e599dfc3645"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_album_ref_accepts_id_mbid_and_url():
    assert album_ref(6214) == "6214"
    assert album_ref(RG.upper()) == RG
    assert album_ref(f"https://music.example.lan/album/{RG}") == RG
    assert album_ref("https://music.example.lan/library/albums/42/") == "42"
    with pytest.raises(ToolError):
        album_ref("not an album")


class FakeAurral:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.op_polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p, m = request.url.path.removeprefix("/api").removeprefix("/library"), request.method
        album = {"id": 1, "artistId": 7, "title": "Robot Face", "releaseGroupMbid": RG}
        if m == "GET" and p in (f"/albums/{RG}", "/albums/1"):
            return httpx.Response(200, json=album)
        if p == "/albums/1/status":
            return httpx.Response(
                200,
                json={
                    "status": "partial",
                    "releaseDate": "2015",
                    "counts": {"total": 2, "available": 1, "missing": 1},
                },
            )
        if p == "/albums" and m == "GET":
            return httpx.Response(
                200, json=[album, {"id": 2, "title": "robot  face", "releaseGroupMbid": "other"}]
            )
        if p == "/tracks":
            return httpx.Response(
                200,
                json=[
                    {"id": "10", "title": "A", "trackNumber": 1, "hasFile": True, "size": 2e6},
                    {"id": "11", "title": "B", "trackNumber": 2, "hasFile": False},
                ],
            )
        if p == "/tracks/10/files":
            return httpx.Response(200, json={"paths": ["/m/Artist/Album/01 - A.flac"]})
        if p == "/files/ingest/check":
            return httpx.Response(200, json={"files": 2})
        if p == "/files/ingest":
            return httpx.Response(200, json={"operation": {"id": 9, "status": "planning"}})
        if p == "/files/operations/9":
            self.op_polls += 1
            return httpx.Response(
                200,
                json={
                    "operation": {
                        "id": 9,
                        "status": "complete",
                        "counts": {"done": 1, "skipped": 1},
                    }
                },
            )
        if p == "/files/operations/9/items":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "status": "done",
                            "source": "a.flac",
                            "target": "Artist/Album/01 - A.flac",
                            "hardlinked": True,
                        },
                        {"status": "skipped", "source": "b.flac", "reason": "duplicate"},
                    ]
                },
            )
        if p == "/refresh":
            return httpx.Response(202, json={"queued": True, "jobId": 5})
        if p == "/refresh/5":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json={})

    def find(self, method, path):
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path)]


async def _server(fake):
    server = MCPServer("t")
    aurral_tools.register(server, AurralClient(SVC, "k", transport=httpx.MockTransport(fake)))
    return server


async def _call(server, name, **args):
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_album_summary_from_url():
    fake = FakeAurral()
    out = await _call(await _server(fake), "aurral_album", album=f"https://x/album/{RG}")
    assert out["id"] == "1" and out["counts"] == {"total": 2, "available": 1, "missing": 1}
    assert out["folders"] == {"/m/Artist/Album": 1}
    assert [s["id"] for s in out["same_title_albums"]] == [2]
    assert out["tracks"][0]["file"] == "01 - A.flac" and out["tracks"][1]["file"] is None
    assert fake.requests[0].headers["X-Api-Key"] == "k"


@pytest.mark.anyio
async def test_import_dry_run_only_checks(monkeypatch):
    fake = FakeAurral()
    out = await _call(await _server(fake), "aurral_import_folder", source_path="/dl/Album")
    assert out["dry_run"] and not fake.find("POST", "/files/ingest")


@pytest.mark.anyio
async def test_import_runs_waits_and_reports_leftovers(monkeypatch):
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(aurral_tools.asyncio, "sleep", no_sleep)
    fake = FakeAurral()
    out = await _call(
        await _server(fake), "aurral_import_folder", source_path="/dl/Album", dry_run=False
    )
    (ingest,) = fake.find("POST", "/files/ingest")
    assert json.loads(ingest.content) == {
        "sourcePath": "/dl/Album",
        "mode": "hardlink",
        "monitor": "none",
        "fillTags": True,
    }
    assert out["status"] == "complete" and "hardlinked" not in out
    assert out["targets"] == ["Artist/Album"]
    assert out["not_imported"] == [{"source": "b.flac", "status": "skipped", "reason": "duplicate"}]


@pytest.mark.anyio
async def test_refresh_sends_numeric_id_and_waits():
    fake = FakeAurral()
    out = await _call(await _server(fake), "aurral_refresh", album=RG)
    (refresh,) = fake.find("POST", "/refresh")
    assert json.loads(refresh.content) == {"mode": "quick", "albumId": 1}
    assert out == {"job": 5, "status": "completed"}


@pytest.mark.anyio
async def test_set_monitored_resolves_album():
    fake = FakeAurral()
    await _call(await _server(fake), "aurral_set_monitored", album=RG, monitored=False)
    (put,) = fake.find("PUT", "/albums/1")
    assert json.loads(put.content) == {"monitored": False}


@pytest.mark.anyio
async def test_errors_carry_aurrals_message():
    def handler(request):
        return httpx.Response(404, json={"error": "Album not found"})

    server = MCPServer("t")
    aurral_tools.register(server, AurralClient(SVC, "k", transport=httpx.MockTransport(handler)))
    # Called directly the server raises; over MCP the client gets the message
    # as an error result.
    with pytest.raises(ToolError, match="404 Album not found"):
        await server.call_tool("aurral_album", {"album": RG})


@pytest.mark.anyio
async def test_search_summarises_artists_and_albums():
    def handler(request):
        assert request.url.path == "/api/search/unified"
        return httpx.Response(
            200,
            json={
                "artists": [{"name": "E", "id": "a1", "hasMbid": True, "inLibrary": False}],
                "albums": [
                    {
                        "title": "E",
                        "artistName": "E",
                        "id": RG,
                        "primaryType": "Album",
                        "inLibrary": True,
                        "libraryAlbumId": 9,
                    }
                ],
            },
        )

    server = MCPServer("t")
    aurral_tools.register(server, AurralClient(SVC, "k", transport=httpx.MockTransport(handler)))
    out = await _call(server, "aurral_search", query="e")
    assert out["artists"][0]["mbid"] == "a1"
    assert out["albums"][0] == {
        "title": "E",
        "artist": "E",
        "release_group": RG,
        "type": "Album",
        "secondary": None,
        "date": None,
        "in_library": True,
        "library_id": 9,
    }
