"""chaptarr_import_file: upload through FileBrowser, match with Chaptarr's
manual import, import only on a clean match and only when asked."""

import hashlib
import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp import server as srv
from jtec_mcp.arr import chaptarr_import
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.config import ServiceConfig, load
from jtec_mcp.errors import ToolError
from jtec_mcp.filebrowser.client import FilebrowserClient

OPTS = {
    "source": "books",
    "upload_dir": "/downloads/in",
    "chaptarr_dir": "/data/books/downloads/in",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def candidate(path, rejections=()):
    return {
        "path": path,
        "author": {"id": 1, "authorName": "China Miéville"},
        "book": {"id": 82, "title": "The Rouse", "mediaType": "ebook", "hasFiles": False},
        "edition": {
            "id": 715,
            "isbn13": "9781509847426",
            "publisher": "Picador",
            "format": "ebook",
        },
        "editionId": 715,
        "quality": {"quality": {"id": 3, "name": "EPUB"}, "revision": {"version": 1}},
        "indexerFlags": 0,
        "rejections": [{"reason": r, "type": "permanent"} for r in rejections],
        "warnings": [],
        "disableReleaseSwitching": False,
    }


class Fake:
    """Both services behind one transport, told apart by host."""

    def __init__(self, existing_sha=None, rejections=()):
        self.existing_sha = existing_sha
        self.rejections = rejections
        self.uploads: list[httpx.Request] = []
        self.commands: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path, m = request.url.host, request.url.path, request.method
        if host == "files.test":
            assert request.headers["authorization"] == "Bearer tok"
            assert request.url.params["source"] == "books"
            if m == "GET":
                assert request.url.params["checksum"] == "sha256"
                if self.existing_sha is None:
                    return httpx.Response(404, json={"status": 404, "message": "no such file"})
                # size is block-rounded in real responses; the tool must not use it
                return httpx.Response(
                    200,
                    json={"name": "x", "size": 4096, "checksums": {"sha256": self.existing_sha}},
                )
            self.uploads.append(request)
            return httpx.Response(200)
        assert host == "chaptarr.test"
        if path == "/api/v1/manualimport":
            folder = request.url.params["folder"]
            assert folder == "/data/books/downloads/in"
            return httpx.Response(
                200,
                json=[
                    candidate(f"{folder}/other.epub"),
                    candidate(f"{folder}/The Rouse.epub", self.rejections),
                ],
            )
        if path == "/api/v1/command" and m == "POST":
            body = json.loads(request.content)
            self.commands.append(body)
            return httpx.Response(201, json={"id": 5, "name": body["name"], "status": "completed"})
        raise AssertionError(f"unexpected {m} {request.url}")


def build(fake, opts=OPTS):
    t = httpx.MockTransport(fake)
    chaptarr = ArrClient(ServiceConfig("chaptarr", "https://chaptarr.test", "C"), "k", t)
    fb = FilebrowserClient(
        ServiceConfig("filebrowser", "https://files.test", "F", options=opts), "tok", t
    )
    server = MCPServer("t")
    chaptarr_import.register(server, chaptarr, fb)
    return server


@pytest.fixture
def book(tmp_path):
    p = tmp_path / "The Rouse.epub"
    p.write_bytes(b"epub" * 100)
    return p


async def call(server, **args):
    result = await server.call_tool("chaptarr_import_file", args)
    return result, (None if result.is_error else json.loads(result.content[0].text))


@pytest.mark.anyio
async def test_dry_run_uploads_and_matches_without_importing(book):
    fake = Fake()
    _, out = await call(build(fake), file=str(book))
    assert out["upload"] == "uploaded" and out["imported"] is False and out["would_import"]
    assert out["match"]["book"]["id"] == 82 and out["match"]["quality"] == "EPUB"
    (up,) = fake.uploads
    assert up.url.params["path"] == "/downloads/in/The Rouse.epub"
    assert up.url.params["override"] == "false" and up.content == book.read_bytes()
    assert fake.commands == []


@pytest.mark.anyio
async def test_import_sends_the_ui_shaped_file_entry(book):
    fake = Fake()
    _, out = await call(build(fake), file=str(book), dry_run=False, wait_s=0)
    assert out["imported"] is True
    (cmd,) = fake.commands
    assert cmd["name"] == "ManualImport" and cmd["importMode"] == "move"
    assert cmd["replaceExistingFiles"] is False
    (f,) = cmd["files"]
    assert f["path"] == "/data/books/downloads/in/The Rouse.epub"
    assert (f["authorId"], f["bookId"], f["editionId"]) == (1, 82, 715)
    assert f["selectionSource"] == "automatic" and f["quality"]["quality"]["name"] == "EPUB"


@pytest.mark.anyio
async def test_same_file_is_not_uploaded_again(book):
    fake = Fake(existing_sha=hashlib.sha256(book.read_bytes()).hexdigest())
    _, out = await call(build(fake), file=str(book))
    assert out["upload"] == "already there" and fake.uploads == []


@pytest.mark.anyio
async def test_different_file_with_same_name_is_an_error(book):
    fake = Fake(existing_sha="0" * 64)
    with pytest.raises(ToolError, match="different contents"):
        await call(build(fake), file=str(book))
    assert fake.uploads == []


@pytest.mark.anyio
async def test_rejected_match_is_not_imported_even_when_asked(book):
    fake = Fake(rejections=["Not an upgrade for existing book file(s)"])
    _, out = await call(build(fake), file=str(book), dry_run=False)
    assert out["imported"] is False and out["would_import"] is False
    assert out["problems"] == ["Not an upgrade for existing book file(s)"]
    assert fake.commands == []


@pytest.mark.anyio
async def test_missing_local_file(tmp_path):
    with pytest.raises(ToolError, match="not a file"):
        await call(build(Fake()), file=str(tmp_path / "nope.epub"))


@pytest.mark.anyio
async def test_not_registered_without_chaptarr_dir():
    server = build(Fake(), opts={"source": "books", "upload_dir": "/in"})
    assert "chaptarr_import_file" not in {t.name for t in await server.list_tools()}


@pytest.mark.anyio
async def test_registered_only_with_both_services(monkeypatch):
    monkeypatch.setenv("C", "k")
    monkeypatch.setenv("F", "tok")
    chaptarr = ServiceConfig("chaptarr", "https://chaptarr.test", "C")
    fb = ServiceConfig("filebrowser", "https://files.test", "F", options=OPTS)

    async def names(services):
        return {t.name for t in await srv.build(services).list_tools()}

    assert "chaptarr_import_file" in await names({"chaptarr": chaptarr, "filebrowser": fb})
    assert "chaptarr_import_file" not in await names({"chaptarr": chaptarr})
    assert "chaptarr_import_file" not in await names({"filebrowser": fb})


def test_service_options_come_from_extra_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[services.filebrowser]\nurl = "https://files.test/"\nsource = "books"\n'
        'upload_dir = "/in"\nchaptarr_dir = "/data/in"\n'
    )
    fb = load(p)["filebrowser"]
    assert fb.url == "https://files.test"
    assert fb.options == {"source": "books", "upload_dir": "/in", "chaptarr_dir": "/data/in"}
