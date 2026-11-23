import httpx
import pytest

from jtec_mcp.config import ServiceConfig
from jtec_mcp.server import build
from jtec_mcp.slskd.client import SlskdClient

SVC = ServiceConfig("slskd", "https://slskd.test", "SLSKD")


@pytest.mark.anyio
async def test_search_polls_until_complete_then_reads_responses():
    polls = {"n": 0}
    seen_key = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_key.add(request.headers.get("X-API-Key"))
        path = request.url.path
        if request.method == "POST" and path == "/api/v0/searches":
            return httpx.Response(200, json={"id": "x"})
        if path.endswith("/responses"):
            return httpx.Response(200, json=[{"username": "u", "files": []}])
        polls["n"] += 1
        return httpx.Response(200, json={"isComplete": polls["n"] >= 2})

    client = SlskdClient(SVC, "secret", transport=httpx.MockTransport(handler))
    _, responses, completed = await client.search("q", poll_s=0)
    assert completed and polls["n"] == 2 and responses[0]["username"] == "u"
    assert seen_key == {"secret"}


@pytest.mark.anyio
async def test_tools_registered_only_with_key(monkeypatch):
    monkeypatch.delenv("SLSKD", raising=False)
    assert [t.name for t in await build({"slskd": SVC}).list_tools()] == []
    monkeypatch.setenv("SLSKD", "k")
    names = {t.name for t in await build({"slskd": SVC}).list_tools()}
    assert names == {
        "slskd_search_album",
        "slskd_downloads",
        "slskd_conversations",
        "slskd_download_folder",
        "slskd_clear_downloads",
        "slskd_reply",
        "slskd_acknowledge",
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _search_payload():
    return [
        {
            "username": "peer",
            "hasFreeUploadSlot": True,
            "queueLength": 0,
            "uploadSpeed": 1,
            "files": [
                {"filename": "m\\Album\\01 A.flac", "size": 10},
                {"filename": "m\\Album\\02 B.flac", "size": 20},
                {"filename": "m\\Album\\cover.jpg", "size": 1},
            ],
        }
    ]


class FakeSlskd:
    """Records requests; answers the endpoints the tools use."""

    def __init__(self, downloads=None):
        self.requests: list[httpx.Request] = []
        self.downloads = downloads or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/v0/searches":
            return httpx.Response(200, json={})
        if path.endswith("/responses"):
            return httpx.Response(200, json=_search_payload())
        if path.startswith("/api/v0/searches/"):
            return httpx.Response(200, json={"isComplete": True})
        if method == "POST" and path.startswith("/api/v0/transfers/downloads/"):
            import json

            files = json.loads(request.content)
            return httpx.Response(200, json={"enqueued": files, "failed": []})
        if method == "GET" and path == "/api/v0/transfers/downloads":
            return httpx.Response(200, json=self.downloads)
        return httpx.Response(204)

    def calls(self, method):
        return [r for r in self.requests if r.method == method]


async def _tools(fake):
    from mcp.server.mcpserver import MCPServer

    from jtec_mcp.slskd.tools import register

    server = MCPServer("t")
    register(server, SlskdClient(SVC, "k", transport=httpx.MockTransport(fake)))
    return server


async def _call(server, name, **args):
    import json

    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_download_folder_by_number_and_by_path(monkeypatch):
    import json

    import jtec_mcp.slskd.client as c

    monkeypatch.setattr(c.asyncio, "sleep", lambda *_: _noop())
    fake = FakeSlskd()
    server = await _tools(fake)
    found = await _call(server, "slskd_search_album", query="q", track_titles=["A", "B"])
    out = await _call(server, "slskd_download_folder", search_id=found["search_id"], n=1)
    assert out["requested"] == 3 and out["enqueued"] == 3 and out["failed"] == []
    body = json.loads(fake.calls("POST")[-1].content)
    assert {f["filename"] for f in body} >= {"m\\Album\\01 A.flac"} and body[0]["size"] == 10

    out = await _call(
        server,
        "slskd_download_folder",
        search_id="other",
        username="peer",
        folder="m\\Album",
        audio_only=True,
    )
    assert out["requested"] == 2


async def _noop():
    return None


@pytest.mark.anyio
async def test_clear_downloads_dry_run_then_removes_only_finished():
    downloads = [
        {
            "username": "peer",
            "directories": [
                {
                    "directory": "d",
                    "files": [
                        {"id": "1", "state": "Completed, Succeeded"},
                        {"id": "2", "state": "Completed, Errored"},
                        {"id": "3", "state": "InProgress"},
                    ],
                }
            ],
        }
    ]
    fake = FakeSlskd(downloads)
    server = await _tools(fake)
    out = await _call(server, "slskd_clear_downloads")
    assert out["dry_run"] and out["would_remove"] == 2 and not fake.calls("DELETE")
    out = await _call(server, "slskd_clear_downloads", failed_only=True, dry_run=False)
    assert out["removed"] == 1
    (delete,) = fake.calls("DELETE")
    assert delete.url.path.endswith("/peer/2") and delete.url.params["remove"] == "true"


@pytest.mark.anyio
async def test_reply_sends_json_string_and_acknowledges():
    import json

    fake = FakeSlskd()
    server = await _tools(fake)
    await _call(server, "slskd_reply", username="a b", message="cheese")
    post, put = fake.calls("POST")[-1], fake.calls("PUT")[-1]
    assert post.url.raw_path.endswith(b"/conversations/a%20b")
    assert json.loads(post.content) == "cheese"
    assert put.url.raw_path.endswith(b"/conversations/a%20b")


@pytest.mark.anyio
async def test_conversations_last_messages_zero_lists_none():
    class Chats(FakeSlskd):
        def __call__(self, request):
            if request.url.path == "/api/v0/conversations":
                return httpx.Response(
                    200, json=[{"username": "u", "hasUnAcknowledgedMessages": True}]
                )
            if request.url.path == "/api/v0/conversations/u":
                return httpx.Response(200, json={"messages": [{"message": "hi"}]})
            return super().__call__(request)

    server = await _tools(Chats())
    (chat,) = await _call(server, "slskd_conversations", last_messages=0)
    assert chat["messages"] == []
