"""Prowlarr tools: indexer status, tests, and enabling or disabling an indexer."""

from __future__ import annotations

import json

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.arr.client import ArrClient
from jtec_mcp.errors import ToolError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
TEST = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
# An indexer behind FlareSolverr can take over a minute per test.
TEST_TIMEOUT = 180

CHANGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _failures(result: dict) -> list[str] | None:
    msgs = [f.get("errorMessage") for f in result.get("validationFailures") or []]
    return [m for m in msgs if m] or None


def register(server: MCPServer, c: ArrClient) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def prowlarr_indexers() -> str:
        """Prowlarr's indexers: id, enabled, tags (e.g. a VPN proxy or
        FlareSolverr) and, for failing ones, when the failures began and until
        when Prowlarr is backing off. Ids feed prowlarr_test and
        prowlarr_set_indexer.
        """
        indexers, status, tags = await c.indexers(), await c.indexer_status(), await c.tags()
        by_id = {s["indexerId"]: s for s in status}
        out = []
        for i in sorted(indexers, key=lambda i: i.get("name", "").lower()):
            row = {
                "id": i["id"],
                "name": i.get("name"),
                "enabled": i.get("enable"),
                "tags": [tags.get(t, t) for t in i.get("tags") or []] or None,
            }
            if s := by_id.get(i["id"]):
                row["failing_since"] = s.get("initialFailure")
                row["backoff_until"] = s.get("disabledTill")
            out.append(row)
        return _compact(out)

    @server.tool(annotations=TEST, structured_output=False)
    async def prowlarr_test(indexer_id: int = 0, apps: bool = False) -> str:
        """Test Prowlarr's indexers, or one indexer, or its app connections.

        With no `indexer_id`, tests every enabled indexer; a passing test also
        ends a failure backoff, e.g. after the VPN or FlareSolverr was down.
        `indexer_id` tests one indexer, enabled or not (use it before
        re-enabling). `apps=true` tests the connections to Sonarr, Radarr and
        the others instead.
        """
        if apps:
            results = await c.send("POST", "/applications/testall", timeout=TEST_TIMEOUT)
            names = {a["id"]: a.get("name") for a in await c.get("/applications")}
        elif indexer_id:
            body = await c.get(f"/indexer/{indexer_id}")
            try:
                await c.send("POST", "/indexer/test", json=body, timeout=TEST_TIMEOUT)
                return _compact([{"id": indexer_id, "name": body.get("name"), "ok": True}])
            except httpx.TimeoutException:
                return _compact(
                    [
                        {
                            "id": indexer_id,
                            "name": body.get("name"),
                            "ok": False,
                            "error": f"no answer within {TEST_TIMEOUT}s",
                        }
                    ]
                )
            except ToolError as e:
                return _compact(
                    [{"id": indexer_id, "name": body.get("name"), "ok": False, "error": str(e)}]
                )
        else:
            try:
                results = await c.send("POST", "/indexer/testall", timeout=TEST_TIMEOUT)
            except httpx.TimeoutException as e:
                raise ToolError(
                    f"Prowlarr's test of all indexers took over {TEST_TIMEOUT}s; "
                    "test the slow ones one at a time"
                ) from e
            names = {i["id"]: i.get("name") for i in await c.indexers()}
        return _compact(
            [
                {
                    "id": r.get("id"),
                    "name": names.get(r.get("id")),
                    "ok": r.get("isValid"),
                    "errors": _failures(r),
                }
                for r in results or []
            ]
        )

    @server.tool(annotations=CHANGE, structured_output=False)
    async def prowlarr_set_indexer(indexer_id: int, enabled: bool, force: bool = False) -> str:
        """Enable or disable a Prowlarr indexer.

        Prowlarr tests an indexer when it is saved, so enabling one that fails
        its test is refused with the test's error (run prowlarr_test first).
        `force=true` saves it anyway. Prowlarr then syncs the change to the
        apps.
        """
        body = await c.get(f"/indexer/{indexer_id}")
        if body.get("enable") == enabled:
            return _compact({"id": indexer_id, "name": body.get("name"), "unchanged": True})
        body["enable"] = enabled
        params = {"forceSave": "true"} if force else None
        saved = await c.send(
            "PUT", f"/indexer/{indexer_id}", params=params, json=body, timeout=TEST_TIMEOUT
        )
        return _compact(
            {"id": indexer_id, "name": saved.get("name"), "enabled": saved.get("enable")}
        )
