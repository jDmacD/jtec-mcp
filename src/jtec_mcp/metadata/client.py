"""Client for the Lidarr metadata server (a MusicBrainz mirror, e.g.
https://api.lidarr.audio/api/v0.4). Public and keyless."""

from __future__ import annotations

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import check


class MetadataClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=service.url,
            headers={"User-Agent": "jtec-mcp"},
            verify=service.verify_tls,
            timeout=30,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        return check(await self._http.get(path, params=params or None), "metadata").json()

    async def search_albums(self, query: str) -> list[dict]:
        return await self._get("/search", type="album", query=query)

    async def album(self, release_group: str) -> dict:
        return await self._get(f"/album/{release_group}")

    async def artist(self, artist_mbid: str) -> dict:
        return await self._get(f"/artist/{artist_mbid}")
