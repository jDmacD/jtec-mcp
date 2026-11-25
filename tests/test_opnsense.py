"""OPNsense client and tools against a fake API."""

import base64
import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError
from jtec_mcp.opnsense import summary
from jtec_mcp.opnsense import tools as opn_tools
from jtec_mcp.opnsense.client import OpnsenseClient, parse_credentials
from jtec_mcp.server import build


@pytest.fixture
def anyio_backend():
    return "asyncio"


SVC = ServiceConfig("opnsense", "https://opnsense.test", "OPNSENSE")

LEASES = [
    {
        "address": "192.0.2.22",
        "hwaddr": "02:00:00:00:00:22",
        "hostname": "mac",
        "state": 0,
        "lease_type": "dynamic",
        "mac_info": "",
        "expire": 1795691661,
    },
    {"address": "192.0.2.30", "hwaddr": "02:00:00:00:00:30", "hostname": "ipad", "state": 0},
]
RESERVATIONS = [
    {
        "uuid": "r1",
        "ip_address": "192.0.2.26",
        "hw_address": "02:00:00:00:00:26",
        "hostname": "laptop",
        "description": "laptop (Wi-Fi)",
    }
]
OVERRIDES = [
    {
        "uuid": "h1",
        "hostname": "laptop",
        "domain": "lan",
        "rr": "A",
        "server": "192.0.2.26",
        "enabled": "1",
    }
]
SUBNETS = [{"uuid": "s1", "subnet": "192.0.2.0/24"}]
QUERIES = [
    {
        "time": 1000,
        "domain": "events.example.com.",
        "action": "Block",
        "blocklist": "AdGuard",
        "rcode": "NOERROR",
    },
    {
        "time": 990,
        "domain": "gateway.filter.example.",
        "action": "Pass",
        "blocklist": None,
        "rcode": "NOERROR",
    },
    {
        "time": 980,
        "domain": "gateway.filter.example.",
        "action": "Pass",
        "blocklist": None,
        "rcode": "NOERROR",
    },
]
FIREWALL = [
    {
        "__timestamp__": "2026-11-25T11:00:01",
        "label": "Default deny",
        "action": "block",
        "interface": "bridge0",
        "dir": "in",
        "src": "192.0.2.22",
        "dst": "198.51.100.6",
        "protoname": "tcp",
        "dstport": "443",
        "tcpflags": "PA",
    },
    {
        "__timestamp__": "2026-11-25T11:00:02",
        "label": "Default deny",
        "action": "block",
        "interface": "bridge0",
        "dir": "in",
        "src": "192.0.2.22",
        "dst": "198.51.100.6",
        "protoname": "tcp",
        "dstport": "443",
        "tcpflags": "S",
    },
    {
        "__timestamp__": "2026-11-25T11:00:03",
        "label": "allow LAN",
        "action": "pass",
        "interface": "bridge0",
        "dir": "in",
        "src": "192.0.2.30",
        "dst": "198.51.100.7",
        "protoname": "udp",
        "dstport": "53",
    },
]


class FakeOpnsense:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            self.posts.append((p, body))
        rows = {
            "/kea/leases4/search": LEASES,
            "/kea/dhcpv4/search_reservation": RESERVATIONS,
            "/kea/dhcpv4/search_subnet": SUBNETS,
            "/unbound/settings/search_host_override": OVERRIDES,
            "/unbound/overview/search_queries": QUERIES,
        }
        if p in rows:
            return httpx.Response(200, json={"rows": rows[p], "total": len(rows[p])})
        if p == "/unbound/diagnostics/test_blocklist":
            if body["domain"].startswith("collector."):
                return httpx.Response(
                    200,
                    json={
                        "status": "OK",
                        "action": "Block",
                        "policy": {"bl": "AdGuard", "description": "default"},
                    },
                )
            return httpx.Response(200, json={"status": "OK", "action": "Pass"})
        if p == "/diagnostics/firewall/log":
            return httpx.Response(200, json=FIREWALL)
        if p in ("/kea/dhcpv4/add_reservation", "/unbound/settings/add_host_override"):
            return httpx.Response(
                200, json={"result": "saved", "uuid": "new-" + p.rsplit("_", 1)[-1]}
            )
        if p.endswith("/service/reconfigure"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"message": f"no route {p}"})


def make(fake=None):
    fake = fake or FakeOpnsense()
    c = OpnsenseClient(SVC, "k:s", transport=httpx.MockTransport(fake))
    server = MCPServer("t")
    opn_tools.register(server, c)
    return server, fake


async def call(server, tool, **args):
    result = await server.call_tool(tool, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_credentials_formats():
    assert parse_credentials('{"key": "a", "secret": "b"}') == ("a", "b")
    assert parse_credentials("key=a\nsecret=b\n") == ("a", "b")
    assert parse_credentials("key: a\nsecret: b") == ("a", "b")
    assert parse_credentials("a:b") == ("a", "b")
    with pytest.raises(ToolError):
        parse_credentials("just-a-key")


@pytest.mark.anyio
async def test_basic_auth_sent():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"rows": []})

    c = OpnsenseClient(SVC, '{"key":"a","secret":"b"}', transport=httpx.MockTransport(handler))
    await c.leases()
    assert seen["auth"] == "Basic " + base64.b64encode(b"a:b").decode()


@pytest.mark.anyio
async def test_dns_check_reports_list_for_blocks_only():
    server, _ = make()
    out = await call(server, "opnsense_dns_check", domains=["github.com", "collector.github.com"])
    assert out[0] == {"domain": "github.com", "action": "Pass"}
    assert out[1]["action"] == "Block" and out[1]["blocklist"] == "AdGuard"


@pytest.mark.anyio
async def test_dns_queries_resolves_hostname_and_sends_int_window():
    server, fake = make()
    out = await call(server, "opnsense_dns_queries", device="mac", minutes=10)
    assert out["device"] == "192.0.2.22"
    assert out["domains"][0]["domain"] == "events.example.com"
    assert out["domains"][0]["blocklist"] == "AdGuard"
    assert out["domains"][1]["count"] == 2
    assert out["totals"] == {"Block": 1, "Pass": 2}
    _path, body = next(x for x in fake.posts if x[0] == "/unbound/overview/search_queries")
    assert body["client"] == "192.0.2.22"
    assert isinstance(body["timeStart"], int) and body["timeEnd"] - body["timeStart"] == 600


@pytest.mark.anyio
async def test_client_joins_reservation_and_override():
    server, _ = make()
    out = await call(server, "opnsense_client", query="02-00-00-00-00-26")
    assert len(out) == 1
    assert out[0]["reservation"]["uuid"] == "r1"
    assert out[0]["dns"][0]["name"] == "laptop.lan"
    by_name = await call(server, "opnsense_client", query="ipad")
    assert by_name[0]["ip"] == "192.0.2.30" and by_name[0]["reservation"] is None


@pytest.mark.anyio
async def test_firewall_log_groups_and_counts_syns():
    server, _ = make()
    out = await call(server, "opnsense_firewall_log", address="192.0.2.22")
    assert out["matched"] == 2 and out["scanned"] == 3
    flow = out["flows"][0]
    assert flow["count"] == 2 and flow["tcpflags"] == {"PA": 1, "S": 1}
    assert flow["new_connections"] == 1


@pytest.mark.anyio
async def test_pin_host_dry_run_changes_nothing():
    server, fake = make()
    out = await call(
        server, "opnsense_pin_host", mac="aa:bb:cc:dd:ee:ff", ip="192.0.2.40", hostname="printer"
    )
    assert out["dry_run"] and out["conflicts"] == []
    assert out["reservation"]["subnet"] == "192.0.2.0/24"
    assert not [p for p, _ in fake.posts if "add_" in p or "reconfigure" in p]


@pytest.mark.anyio
async def test_pin_host_refuses_conflicts_even_when_not_dry_run():
    server, fake = make()
    out = await call(
        server,
        "opnsense_pin_host",
        mac="aa:bb:cc:dd:ee:ff",
        ip="192.0.2.26",
        hostname="laptop",
        dry_run=False,
    )
    assert out["dry_run"] and len(out["conflicts"]) == 2
    assert not [p for p, _ in fake.posts if "add_" in p]


@pytest.mark.anyio
async def test_pin_host_adds_both_and_reconfigures():
    server, fake = make()
    out = await call(
        server,
        "opnsense_pin_host",
        mac="AA-BB-CC-DD-EE-FF",
        ip="192.0.2.40",
        hostname="printer",
        dry_run=False,
    )
    assert out["reservation_uuid"] and out["host_override_uuid"]
    paths = [p for p, _ in fake.posts if "add_" in p or "reconfigure" in p]
    assert paths == [
        "/kea/dhcpv4/add_reservation",
        "/kea/service/reconfigure",
        "/unbound/settings/add_host_override",
        "/unbound/service/reconfigure",
    ]
    res = dict(fake.posts)["/kea/dhcpv4/add_reservation"]["reservation"]
    assert res == {
        "subnet": "s1",
        "ip_address": "192.0.2.40",
        "hw_address": "aa:bb:cc:dd:ee:ff",
        "hostname": "printer",
        "description": "",
    }
    host = dict(fake.posts)["/unbound/settings/add_host_override"]["host"]
    assert host["server"] == "192.0.2.40" and host["addptr"] == "1" and host["domain"] == "lan"


@pytest.mark.anyio
async def test_refused_save_surfaces_validations():
    class Refusing(FakeOpnsense):
        def __call__(self, request):
            if request.url.path.endswith("add_reservation"):
                return httpx.Response(
                    200, json={"result": "failed", "validations": {"ip_address": "in pool"}}
                )
            return super().__call__(request)

    server, _ = make(Refusing())
    with pytest.raises(ToolError, match="in pool"):
        await server.call_tool(
            "opnsense_pin_host",
            {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.0.2.40", "hostname": "p", "dry_run": False},
        )


@pytest.mark.anyio
async def test_registration_needs_secret(monkeypatch):
    monkeypatch.delenv("OPNSENSE", raising=False)
    assert "opnsense_client" not in await _tool_names(build({"opnsense": SVC}))
    monkeypatch.setenv("OPNSENSE", "k:s")
    assert {"opnsense_dns_check", "opnsense_pin_host"} <= await _tool_names(
        build({"opnsense": SVC})
    )


async def _tool_names(server) -> set[str]:
    return {t.name for t in await server.list_tools()}


def test_match_devices_ip_is_exact():
    found = summary.match_devices("192.0.2.2", LEASES, RESERVATIONS, OVERRIDES)
    assert found == []
