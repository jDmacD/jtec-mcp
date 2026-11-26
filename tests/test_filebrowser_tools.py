"""files_* tools over FileBrowser Quantum: sources, search, list, upload."""

import hashlib
import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp import server as srv
from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError
from jtec_mcp.filebrowser import tools
from jtec_mcp.filebrowser.client import FilebrowserClient

SOURCES = {
    "books": {"status": "ready", "readOnly": False, "numDirs": 3, "numFiles": 6, "used": 100},
    "films": {"status": "indexing", "readOnly": True, "numDirs": 9, "numFiles": 40, "used": 9},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Fake:
    def __init__(self, existing=None):
        self.existing = existing or {}  # (source, path) -> sha256
        self.requests: list[httpx.Request] = []
        self.uploads: list[tuple[str, str, bytes]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        path, p, m = request.url.path, request.url.params, request.method
        if path == "/api/settings/sources":
            return httpx.Response(200, json=SOURCES)
        if path == "/api/tools/search":
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "completed/Dune (2021)/Dune.mkv",
                        "type": "video/x-matroska",
                        "size": 7,
                        "modified": "2026-01-01T00:00:00Z",
                        "source": "films",
                    },
                    {
                        "path": "completed/Dune (2021)/",
                        "type": "directory",
                        "size": 9,
                        "source": "films",
                    },
                ],
            )
        if path == "/api/resources" and m == "GET":
            key = (p["source"], p["path"])
            if key == ("books", "/"):
                return httpx.Response(
                    200,
                    json={
                        "name": "books",
                        "type": "directory",
                        "size": 100,
                        "path": "/",
                        "folders": [
                            {"name": "zeta", "type": "directory", "size": 1},
                            {"name": "Alpha", "type": "directory", "size": 2},
                        ],
                        "files": [
                            {
                                "name": "a.epub",
                                "type": "application/epub+zip",
                                "size": 5,
                                "modified": "2026-11-26T03:58:45.123Z",
                            }
                        ],
                    },
                )
            if key in self.existing:
                assert p["checksum"] == "sha256"
                return httpx.Response(
                    200,
                    json={
                        "name": "x",
                        "type": "text/plain",
                        "size": 4096,
                        "checksums": {"sha256": self.existing[key]},
                    },
                )
            return httpx.Response(404, json={"status": 404, "message": "not found"})
        if path == "/api/resources" and m == "POST":
            assert p["override"] == "false"
            self.uploads.append((p["source"], p["path"], request.read()))
            return httpx.Response(200)
        raise AssertionError(f"unexpected {m} {request.url}")


def build(fake):
    fb = FilebrowserClient(
        ServiceConfig("filebrowser", "https://files.test", "F"), "tok", httpx.MockTransport(fake)
    )
    server = MCPServer("t")
    tools.register(server, fb)
    return server


async def call(server, name, **args):
    result = await server.call_tool(name, args)
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_sources():
    out = await call(build(Fake()), "files_sources")
    assert out == [
        {"name": "books", "status": "ready", "writable": True, "dirs": 3, "files": 6, "bytes": 100},
        {
            "name": "films",
            "status": "indexing",
            "writable": False,
            "dirs": 9,
            "files": 40,
            "bytes": 9,
        },
    ]


@pytest.mark.anyio
async def test_search_defaults_to_every_source_and_puts_folders_first():
    fake = Fake()
    out = await call(build(fake), "files_search", query="dune")
    search = fake.requests[-1]
    assert search.url.params["sources"] == "books,films" and search.url.params["limit"] == "50"
    assert [r["path"] for r in out["results"]] == [
        "/completed/Dune (2021)/",
        "/completed/Dune (2021)/Dune.mkv",
    ]
    assert out["results"][0]["type"] == "dir" and out["maybe_truncated"] is False


@pytest.mark.anyio
async def test_search_kind_and_explicit_sources():
    fake = Fake()
    out = await call(build(fake), "files_search", query="dune", sources=["films"], kind="files")
    assert fake.requests[-1].url.params["sources"] == "films"
    assert [r["type"] for r in out["results"]] == ["video/x-matroska"]


@pytest.mark.anyio
async def test_list_folder_sorts_dirs_then_files():
    out = await call(build(Fake()), "files_list", source="books")
    assert [e["name"] for e in out["entries"]] == ["Alpha", "zeta", "a.epub"]
    assert out["entries"][2]["modified"] == "2026-11-26T03:58:45"
    assert (out["dirs"], out["files"], out["truncated"]) == (2, 1, False)


@pytest.mark.anyio
async def test_list_missing_path():
    with pytest.raises(ToolError, match="doesn't exist"):
        await call(build(Fake()), "files_list", source="books", path="nope")


@pytest.mark.anyio
async def test_upload_streams_to_folder(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"x" * 3_000_000)  # more than one chunk
    fake = Fake()
    out = await call(build(fake), "files_upload", file=str(f), source="incoming", folder="a/b/")
    assert out["upload"] == "uploaded" and out["path"] == "/a/b/notes.txt"
    assert fake.uploads == [("incoming", "/a/b/notes.txt", f.read_bytes())]


@pytest.mark.anyio
async def test_upload_same_file_is_a_no_op_and_different_file_an_error(tmp_path):
    f = tmp_path / "n.txt"
    f.write_bytes(b"hello")
    same = Fake({("incoming", "/n.txt"): hashlib.sha256(b"hello").hexdigest()})
    out = await call(build(same), "files_upload", file=str(f), source="incoming")
    assert out["upload"] == "already there" and same.uploads == []
    other = Fake({("incoming", "/n.txt"): "0" * 64})
    with pytest.raises(ToolError, match="different contents"):
        await call(build(other), "files_upload", file=str(f), source="incoming")


@pytest.mark.anyio
async def test_registered_with_filebrowser(monkeypatch):
    monkeypatch.setenv("F", "tok")
    fb = ServiceConfig("filebrowser", "https://files.test", "F")
    names = {t.name for t in await srv.build({"filebrowser": fb}).list_tools()}
    assert {"files_sources", "files_search", "files_list", "files_upload"} <= names
    assert "chaptarr_import_file" not in names
