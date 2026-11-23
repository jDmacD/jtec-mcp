"""Thin async client for the slskd REST API (v0)."""

from __future__ import annotations

import asyncio
import time
import uuid
from urllib.parse import quote

import httpx

from jtec_mcp.config import ServiceConfig


class SlskdClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api/v0",
            headers={"X-API-Key": api_key},
            verify=service.verify_tls,
            timeout=30,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        r = await self._http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def search(
        self, text: str, timeout_s: float = 90, poll_s: float = 2
    ) -> tuple[str, list[dict], bool]:
        """Start a search, wait for it to finish, return (id, responses, completed)."""
        search_id = str(uuid.uuid4())
        r = await self._http.post("/searches", json={"id": search_id, "searchText": text})
        r.raise_for_status()
        deadline = time.monotonic() + timeout_s
        completed = False
        while time.monotonic() < deadline:
            state = await self._get(f"/searches/{search_id}")
            if state.get("isComplete"):
                completed = True
                break
            await asyncio.sleep(poll_s)
        # Responses are only listed from this endpoint; the search object's own
        # `responses` stays empty while the search runs.
        responses = await self._get(f"/searches/{search_id}/responses")
        return search_id, responses, completed

    async def downloads(self, username: str | None = None) -> list[dict]:
        if username:
            data = await self._get(f"/transfers/downloads/{quote(username, safe='')}")
            return [data] if data else []
        return await self._get("/transfers/downloads")

    async def conversations(self) -> list[dict]:
        return await self._get("/conversations")

    async def conversation(self, username: str) -> dict:
        return await self._get(f"/conversations/{quote(username, safe='')}", includeMessages="true")
