"""Thin async client for the OPNsense API (no published spec; routes are the
`*Action` methods in /usr/local/opnsense/mvc/app/controllers/OPNsense/*/Api/).

Auth is HTTP basic with the API key as user and its secret as password. The
secret may arrive as JSON (`{"key": ..., "secret": ...}`), as `key:secret`, or
as the `key=...` / `secret=...` lines of the apikey.txt file OPNsense offers
when the key is created. Search endpoints take a JSON body (`current`,
`rowCount`, `searchPhrase`) and return `{"rows": [...], "total": n}`; JSON
bodies keep integer types, which `search_queries` needs for its time window.
"""

from __future__ import annotations

import json
import re

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError, check

ALL_ROWS = {"current": 1, "rowCount": -1}


def parse_credentials(raw: str) -> tuple[str, str]:
    """Return (key, secret) from any of the accepted secret formats."""
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and data.get("key") and data.get("secret"):
        return str(data["key"]), str(data["secret"])
    pairs = dict(re.findall(r"^\s*(key|secret)\s*[=:]\s*\"?([^\s\"]+)\"?\s*$", raw, re.MULTILINE))
    if "key" in pairs and "secret" in pairs:
        return pairs["key"], pairs["secret"]
    if raw.count(":") == 1 and "\n" not in raw:
        key, secret = raw.split(":")
        if key and secret:
            return key, secret
    raise ToolError(
        "OPNsense secret must hold an API key and secret (JSON, key:secret or apikey.txt)"
    )


class OpnsenseClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api",
            auth=parse_credentials(api_key),
            verify=service.verify_tls,
            timeout=120,
            transport=transport,
        )

    async def _get(self, path: str, **params):
        r = check(await self._http.get(path, params=params or None), "OPNsense")
        return r.json() if r.content else None

    async def _post(self, path: str, body: dict | None = None):
        r = check(await self._http.post(path, json=body if body is not None else {}), "OPNsense")
        return r.json() if r.content else None

    async def _rows(self, path: str, **body) -> list[dict]:
        return (await self._post(path, {**ALL_ROWS, **body}) or {}).get("rows", [])

    # DNS (Unbound)
    async def test_blocklist(self, domain: str, src: str) -> dict:
        return (
            await self._post("/unbound/diagnostics/test_blocklist", {"domain": domain, "src": src})
            or {}
        )

    async def client_queries(self, client: str, start: int, end: int) -> list[dict]:
        """One client's queries in [start, end] (Unix seconds), newest first.
        OPNsense returns at most the latest 500 in the window."""
        return await self._rows(
            "/unbound/overview/search_queries", client=client, timeStart=start, timeEnd=end
        )

    async def host_overrides(self) -> list[dict]:
        return await self._rows("/unbound/settings/search_host_override")

    async def add_host_override(self, host: dict) -> dict:
        return await self._post("/unbound/settings/add_host_override", {"host": host}) or {}

    async def reconfigure_unbound(self) -> dict:
        return await self._post("/unbound/service/reconfigure") or {}

    # DHCP (Kea)
    async def leases(self) -> list[dict]:
        return await self._rows("/kea/leases4/search")

    async def reservations(self) -> list[dict]:
        return await self._rows("/kea/dhcpv4/search_reservation")

    async def subnets(self) -> list[dict]:
        return await self._rows("/kea/dhcpv4/search_subnet")

    async def add_reservation(self, reservation: dict) -> dict:
        return await self._post("/kea/dhcpv4/add_reservation", {"reservation": reservation}) or {}

    async def reconfigure_kea(self) -> dict:
        return await self._post("/kea/service/reconfigure") or {}

    # Firewall
    async def firewall_log(self, limit: int) -> list[dict]:
        """The latest `limit` filter-log entries across all interfaces, newest first."""
        return await self._get("/diagnostics/firewall/log", limit=limit) or []
