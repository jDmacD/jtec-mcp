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
    assert names == {"slskd_search_album", "slskd_downloads", "slskd_conversations"}


@pytest.fixture
def anyio_backend():
    return "asyncio"
