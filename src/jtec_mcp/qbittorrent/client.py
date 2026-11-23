"""Thin async client for qBittorrent's WebAPI v2, authenticated with the
instance-wide WebAPI key (qBittorrent 5.2+, `app/rotateAPIKey`).

The key only works as `Authorization: Bearer <key>`; an X-Api-Key header gets
403. qBittorrent also validates the Host header, so call it by the name it is
served under (a port-forward to 127.0.0.1 gets 401 even with a good key)."""

from __future__ import annotations

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import check


class QbittorrentClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api/v2",
            headers={"Authorization": f"Bearer {api_key}"},
            verify=service.verify_tls,
            timeout=30,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        r = check(await self._http.get(path, params=params or None), "qBittorrent")
        return r.json() if r.content else None

    async def torrents(self, hashes: list[str] | None = None, filter: str | None = None):
        params = {}
        if hashes:
            params["hashes"] = "|".join(h.lower() for h in hashes)
        if filter:
            params["filter"] = filter
        return await self._get("/torrents/info", **params)

    async def peers(self, torrent_hash: str) -> list[dict]:
        data = await self._get("/sync/torrentPeers", hash=torrent_hash.lower(), rid=0)
        return list((data or {}).get("peers", {}).values())

    async def trackers(self, torrent_hash: str) -> list[dict]:
        return await self._get("/torrents/trackers", hash=torrent_hash.lower())
