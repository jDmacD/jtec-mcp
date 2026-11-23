"""Thin async client for Bazarr's API (no published spec; routes are in
bazarr/api/). Auth is the API key in an X-API-KEY header. Writes take form
fields, not JSON; the read endpoints used here take start/length paging, where
length=-1 means everything."""

from __future__ import annotations

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import check


class BazarrClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api",
            headers={"X-API-KEY": api_key},
            verify=service.verify_tls,
            timeout=120,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        r = check(await self._http.get(path, params=params or None), "Bazarr")
        return r.json() if r.content else None

    async def _all(self, path: str) -> list[dict]:
        return (await self._get(path, start=0, length=-1) or {}).get("data", [])

    async def wanted_movies(self) -> list[dict]:
        return await self._all("/movies/wanted")

    async def wanted_episodes(self) -> list[dict]:
        return await self._all("/episodes/wanted")

    async def movies(self) -> list[dict]:
        return await self._all("/movies")

    async def series(self) -> list[dict]:
        return await self._all("/series")

    async def providers(self) -> list[dict]:
        return (await self._get("/providers") or {}).get("data", [])
