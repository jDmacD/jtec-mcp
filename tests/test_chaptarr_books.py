"""chaptarr_set_book_monitored: find an author's books, change only on request."""

import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.arr import chaptarr_books
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError

AUTHORS = [{"id": 1, "authorName": "China Miéville"}, {"id": 2, "authorName": "China Doe"}]
BOOKS = [
    {"id": 21, "title": "The Rouse", "mediaType": "audiobook", "monitored": False},
    {
        "id": 82,
        "title": "The Rouse",
        "mediaType": "ebook",
        "monitored": True,
        "statistics": {"bookFileCount": 0},
    },
    {"id": 63, "title": "Perdido Street Station", "mediaType": "ebook", "monitored": False},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Fake:
    def __init__(self):
        self.puts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/author":
            return httpx.Response(200, json=AUTHORS)
        if path == "/api/v1/book" and request.method == "GET":
            assert request.url.params["authorId"] == "1"
            return httpx.Response(200, json=BOOKS)
        if path == "/api/v1/book/monitor" and request.method == "PUT":
            self.puts.append(json.loads(request.content))
            return httpx.Response(202, json=[])
        raise AssertionError(f"unexpected {request.method} {request.url}")


def build(fake):
    c = ArrClient(
        ServiceConfig("chaptarr", "https://chaptarr.test", "C"), "k", httpx.MockTransport(fake)
    )
    server = MCPServer("t")
    chaptarr_books.register(server, c)
    return server


async def call(server, **args):
    r = await server.call_tool("chaptarr_set_book_monitored", args)
    return json.loads(r.content[0].text)


@pytest.mark.anyio
async def test_listing_shows_only_monitored_books():
    out = await call(build(Fake()), author="china miéville", monitored=False)
    assert out["books"] == 3 and [b["id"] for b in out["monitored_books"]] == [82]


@pytest.mark.anyio
async def test_dry_run_by_title_and_media_type_changes_nothing():
    fake = Fake()
    out = await call(
        build(fake), author="Miéville", monitored=False, title="rouse", media_type="ebook"
    )
    assert out["dry_run"] is True and out["changing"] == [82] and fake.puts == []


@pytest.mark.anyio
async def test_unmonitor_sends_only_the_books_that_change():
    fake = Fake()
    out = await call(
        build(fake), author="China Miéville", monitored=False, title="rouse", dry_run=False
    )
    assert out["changing"] == [82]
    assert fake.puts == [{"bookIds": [82], "monitored": False}]


@pytest.mark.anyio
async def test_ambiguous_author():
    with pytest.raises(ToolError, match="several authors"):
        await call(build(Fake()), author="china", monitored=False)


@pytest.mark.anyio
async def test_unknown_book_id():
    with pytest.raises(ToolError, match=r"no books \[5\]"):
        await call(build(Fake()), author="Miéville", monitored=True, book_ids=[5])
