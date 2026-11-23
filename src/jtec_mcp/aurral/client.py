"""Thin async client for Aurral's library API (/api/library). No OpenAPI spec is
published; endpoints follow Aurral's backend routes (routes/library/handlers)."""

from __future__ import annotations

import re

import httpx

from jtec_mcp.config import ServiceConfig

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def album_ref(album: str | int) -> str:
    """Accept a library id, a release-group MBID, or an Aurral album URL."""
    text = str(album).strip()
    if text.isdigit():
        return text
    if m := UUID.search(text):
        return m.group(0).lower()
    tail = text.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        return tail
    raise ValueError(f"not an album id, MBID or album URL: {album!r}")


class AurralClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api/library",
            headers={"X-Api-Key": api_key},
            verify=service.verify_tls,
            timeout=60,
            transport=transport,
        )

    async def _request(self, method: str, path: str, **kw):
        r = await self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json()
                detail = detail.get("message") or detail.get("error") or detail
            except ValueError:
                detail = r.text[:200]
            raise RuntimeError(f"Aurral {method} {path}: {r.status_code} {detail}")
        return r.json() if r.content else None

    async def album(self, ref: str) -> dict:
        return await self._request("GET", f"/albums/{ref}")

    async def album_status(self, album_id: str) -> dict:
        return await self._request("GET", f"/albums/{album_id}/status")

    async def artist_albums(self, artist_id: str) -> list[dict]:
        data = await self._request("GET", "/albums", params={"artistId": artist_id})
        return data.get("albums", data) if isinstance(data, dict) else data

    async def tracks(self, album_id: str) -> list[dict]:
        return await self._request("GET", "/tracks", params={"albumId": album_id})

    async def track_files(self, track_id: str) -> list[str]:
        return (await self._request("GET", f"/tracks/{track_id}/files")).get("paths", [])

    async def set_monitored(self, album_id: str, monitored: bool) -> dict:
        return await self._request("PUT", f"/albums/{album_id}", json={"monitored": monitored})

    async def set_edition(self, album_id: str, release_mbid: str) -> dict:
        return await self._request(
            "PUT", f"/albums/{album_id}/edition", json={"releaseMbid": release_mbid}
        )

    async def cancel(self, album_id: str) -> dict:
        return await self._request("POST", f"/albums/{album_id}/cancel")

    async def ingest_check(self, source_path: str) -> dict:
        return await self._request("POST", "/files/ingest/check", json={"sourcePath": source_path})

    async def ingest(self, source_path: str, mode: str, monitor: str, fill_tags: bool) -> dict:
        body = {"sourcePath": source_path, "mode": mode, "monitor": monitor, "fillTags": fill_tags}
        return (await self._request("POST", "/files/ingest", json=body))["operation"]

    async def operation(self, op_id: int) -> dict:
        return (await self._request("GET", f"/files/operations/{op_id}"))["operation"]

    async def operation_items(self, op_id: int, status: str | None = None, limit: int = 500):
        params = {"limit": str(limit)} | ({"status": status} if status else {})
        return (await self._request("GET", f"/files/operations/{op_id}/items", params=params))[
            "items"
        ]

    async def refresh(self, album_id: str | None = None, artist_id: str | None = None) -> dict:
        body: dict = {"mode": "quick"}
        # Aurral rejects ids sent as strings.
        if album_id:
            body["albumId"] = int(album_id)
        elif artist_id:
            body["artistId"] = int(artist_id)
        return await self._request("POST", "/refresh", json=body)

    async def refresh_status(self, job_id) -> dict:
        return await self._request("GET", f"/refresh/{job_id}")
