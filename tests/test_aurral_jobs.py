import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.aurral import tools as aurral_tools
from jtec_mcp.aurral.client import AurralClient
from jtec_mcp.aurral.jobs import match_album_jobs, norm_title, order_jobs, summarise_job
from jtec_mcp.config import ServiceConfig

SVC = ServiceConfig("aurral", "https://aurral.test", "AURRAL")
RG = "1346c819-d57b-4c38-aa55-1e599dfc3645"
BOX = "9e3ea154-566f-40fd-8640-9218a43347b8"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def job(id, status, album="That’s It", mbid=RG, artist="Band", track=1, disc=1, peer=None):
    return {
        "id": id,
        "status": status,
        "albumName": album,
        "albumMbid": mbid,
        "artistName": artist,
        "trackName": f"T{track}",
        "trackNumber": track,
        "discNumber": disc,
        "remoteUsername": peer,
        "downloadSource": "slskd",
    }


JOBS = [
    job("a", "done", peer="p1", track=2),
    job("b", "downloading", peer="p2", track=3),
    # Old request job: no MBID, straight apostrophe, different case.
    job("c", "pending", album="that's it", mbid=None, track=4),
    # Box set containing the album: different title and MBID.
    job("d", "downloading", album="That’s It / Other", mbid=BOX, peer="p3"),
    job("e", "blocked", track=5),
    job("f", "pending", album="That's It", mbid=None, artist="Someone Else"),
]


def test_norm_title_ignores_case_space_and_curly_quotes():
    assert norm_title("That’s  What I’m Not") == norm_title("that's what i'm not")


def test_match_by_mbid_or_title_not_box_set():
    ids = [j["id"] for j in match_album_jobs(JOBS, RG, "That’s It", "band")]
    assert ids == ["a", "b", "c", "e"]
    # Without an artist, a title match takes other artists' albums too.
    assert "f" in [j["id"] for j in match_album_jobs(JOBS, None, "That's It")]
    assert [j["id"] for j in match_album_jobs(JOBS, BOX)] == ["d"]


def test_order_and_summary():
    assert [j["id"] for j in order_jobs(JOBS[:3])] == ["b", "c", "a"]
    s = summarise_job(JOBS[0])
    assert s == {
        "id": "a",
        "status": "done",
        "pos": "1.2",
        "track": "T2",
        "album": "That’s It",
        "source": "slskd",
        "peer": "p1",
    }


class FakeAurral:
    def __init__(self, album_found=True):
        self.requests: list[httpx.Request] = []
        self.album_found = album_found

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p, m = request.url.path.removeprefix("/api"), request.method
        album = {"id": 1, "title": "That’s It", "releaseGroupMbid": RG}
        if m == "GET" and p in (f"/library/albums/{RG}", "/library/albums/1"):
            if not self.album_found:
                return httpx.Response(404, json={"error": "Album not found"})
            return httpx.Response(200, json=album)
        if p == "/library/albums/1/cancel":
            return httpx.Response(200, json={"cancelledJobIds": []})
        if m == "GET" and p == "/playlists/jobs":
            status = request.url.params.get("status")
            return httpx.Response(
                200, json=[j for j in JOBS if not status or j["status"] == status]
            )
        if m == "POST" and p.startswith("/playlists/jobs/") and p.endswith("/cancel"):
            if "/b/" in p:
                return httpx.Response(409, json={"error": "Track download is already finished"})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unexpected"})

    def posted(self):
        return [r.url.path for r in self.requests if r.method == "POST"]


async def _call(fake, name, **args):
    server = MCPServer("t")
    aurral_tools.register(server, AurralClient(SVC, "k", transport=httpx.MockTransport(fake)))
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_album_jobs_lists_matches_active_first():
    out = await _call(FakeAurral(), "aurral_album_jobs", album=RG, artist="Band")
    assert out["album_id"] == 1
    assert [j["id"] for j in out["jobs"]] == ["b", "c", "e", "a"]
    assert out["counts"] == {"downloading": 1, "pending": 1, "blocked": 1, "done": 1}
    assert out["peers"] == 2


@pytest.mark.anyio
async def test_album_jobs_by_title_when_album_gone():
    out = await _call(FakeAurral(), "aurral_album_jobs", album="that's it", artist="band")
    assert out["album_id"] is None
    assert {j["id"] for j in out["jobs"]} == {"a", "b", "c", "e"}


@pytest.mark.anyio
async def test_cancel_sweeps_unlinked_jobs_and_reports_blocked():
    fake = FakeAurral()
    out = await _call(fake, "aurral_cancel_album_downloads", album=RG, artist="Band")
    assert fake.posted() == [
        "/api/library/albums/1/cancel",
        "/api/playlists/jobs/b/cancel",
        "/api/playlists/jobs/c/cancel",
    ]
    assert out["jobs_cancelled"] == ["c"]
    assert out["failed"][0]["id"] == "b"
    assert [j["id"] for j in out["blocked_left"]] == ["e"]


@pytest.mark.anyio
async def test_cancel_by_mbid_when_album_row_gone():
    fake = FakeAurral(album_found=False)
    out = await _call(fake, "aurral_cancel_album_downloads", album=RG)
    assert out["album_id"] is None
    assert "/api/library/albums/1/cancel" not in fake.posted()
    assert "/api/playlists/jobs/b/cancel" in fake.posted()
