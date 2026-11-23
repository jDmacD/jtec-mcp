import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.config import ServiceConfig
from jtec_mcp.metadata.client import MetadataClient
from jtec_mcp.metadata.tools import register
from jtec_mcp.server import build

SVC = ServiceConfig("metadata", "https://meta.test/api/v0.4", "METADATA")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def release(rid, count, fmt, titles):
    return {
        "Id": rid,
        "TrackCount": count,
        "Media": [{"Format": fmt}],
        "Tracks": [
            {
                "TrackName": t,
                "TrackPosition": i + 1,
                "MediumNumber": 1,
                "DurationMs": 1000 * (i + 1),
            }
            for i, t in enumerate(titles)
        ],
    }


ALBUM = {
    "id": "rg",
    "title": "Album",
    "Releases": [
        release("vinyl", 2, "Vinyl", ["A", "B"]),
        release("deluxe", 3, "CD", ["A", "B", "C"]),
        release("cd", 2, "CD", ["B", "A"]),
    ],
}


def handler(request):
    if request.url.path.endswith("/search"):
        return httpx.Response(200, json=[{"id": "rg", "title": "Album", "releasedate": "1995"}])
    return httpx.Response(200, json=ALBUM)


async def _call(name, **args):
    server = MCPServer("t")
    register(server, MetadataClient(SVC, transport=httpx.MockTransport(handler)))
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_standard_edition_is_commonest_count_preferring_cd():
    out = await _call("metadata_album", release_group="rg")
    assert out["track_list_of"] == "cd" and len(out["editions"]) == 3
    assert out["track_list"] == [
        {"pos": "1.1", "title": "B", "seconds": 1},
        {"pos": "1.2", "title": "A", "seconds": 2},
    ]


@pytest.mark.anyio
async def test_explicit_release_and_query():
    out = await _call("metadata_album", release_group="rg", release="deluxe")
    assert [t["title"] for t in out["track_list"]] == ["A", "B", "C"]
    hits = await _call("metadata_album", query="artist album")
    assert hits[0]["release_group"] == "rg"


@pytest.mark.anyio
async def test_keyless_service_registers_without_secret(monkeypatch):
    monkeypatch.delenv("METADATA", raising=False)
    names = {t.name for t in await build({"metadata": SVC}).list_tools()}
    assert names == {"metadata_album"}
