"""Thin async client for Jellyfin's REST API (the server publishes its OpenAPI
spec at /api-docs/openapi.json). Auth is an API key sent as
`Authorization: MediaBrowser Token="<key>"`."""

from __future__ import annotations

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import check

ITEM_FIELDS = "MediaSources,Path"


class JellyfinClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=service.url,
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            verify=service.verify_tls,
            timeout=30,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        r = check(await self._http.get(path, params=params or None), "Jellyfin")
        return r.json() if r.content else None

    async def sessions(self, active_within_s: int = 900) -> list[dict]:
        return await self._get("/Sessions", activeWithinSeconds=active_within_s)

    async def search(self, term: str, types: str = "Movie,Series,Episode", limit: int = 10):
        data = await self._get(
            "/Items",
            searchTerm=term,
            IncludeItemTypes=types,
            Recursive="true",
            Fields=ITEM_FIELDS,
            Limit=limit,
        )
        return data.get("Items", [])

    async def item(self, item_id: str) -> dict | None:
        data = await self._get("/Items", ids=item_id, Fields=ITEM_FIELDS)
        items = data.get("Items", [])
        return items[0] if items else None

    async def episodes(self, series_id: str, season: int | None = None) -> list[dict]:
        params = {"Fields": ITEM_FIELDS}
        if season is not None:
            params["season"] = season
        return (await self._get(f"/Shows/{series_id}/Episodes", **params)).get("Items", [])
