"""OPNsense MCP tools: is a domain blocked, what did a device look up, which
device is this, what did the firewall drop, and pinning a device's address."""

from __future__ import annotations

import asyncio
import json
import time

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.opnsense import summary
from jtec_mcp.opnsense.client import OpnsenseClient

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
PIN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _saved(result: dict, what: str) -> str:
    if result.get("result") != "saved":
        raise ToolError(f"OPNsense refused the {what}: {result.get('validations') or result}")
    return result.get("uuid")


def register(server: MCPServer, client: OpnsenseClient) -> None:
    async def resolve_ip(device: str) -> str:
        """An IP as given, or the single device whose MAC or hostname matches."""
        if summary.is_ip(device):
            return device
        leases, reservations = await asyncio.gather(client.leases(), client.reservations())
        found = [d for d in summary.match_devices(device, leases, reservations, []) if d["ip"]]
        if len(found) != 1:
            names = [(d["ip"], (d["lease"] or {}).get("hostname")) for d in found]
            raise ToolError(f"{device!r} matches {len(found)} devices {names}; pass an IP")
        return found[0]["ip"]

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def opnsense_dns_check(domains: list[str], client_ip: str = "127.0.0.1") -> str:
        """Check whether Unbound's blocklist would block each domain, as seen
        from `client_ip` (policies can be scoped to source networks).

        For each domain: `action` (`Pass` or `Block`), and for blocks the
        list (`bl`, e.g. `ag` = AdGuard, `sb` = Steven Black, `Custom`) and
        the policy name. Parent domains are matched too, so test the exact
        names a site loads (e.g. github.com, octocaptcha.com). To see what a
        device actually looked up, use opnsense_dns_queries.
        """
        if not domains:
            raise ToolError("give at least one domain")

        async def one(d: str) -> dict:
            r = await client.test_blocklist(d.strip().rstrip("."), client_ip)
            out = {"domain": d, "action": r.get("action")}
            policy = r.get("policy") or {}
            if r.get("action") != "Pass":
                out |= {"blocklist": policy.get("bl"), "policy": policy.get("description")}
            return out

        return _compact(await asyncio.gather(*(one(d) for d in domains)))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def opnsense_dns_queries(
        device: str,
        minutes: int = 60,
        blocked_only: bool = False,
        contains: str | None = None,
        limit: int = 60,
    ) -> str:
        """What one device looked up through Unbound, grouped by domain.

        `device` is an IP, MAC or hostname (resolved through DHCP). Covers
        the last `minutes`. OPNsense returns at most the newest 500 queries
        in the window, so for a busy device shorten `minutes`; `truncated`
        says when the cap was hit. `blocked_only` keeps blocked/dropped
        domains; `contains` keeps domains containing that text. Blocked
        domains come first, then by count; `totals` counts every query by
        action. `time`s are Unix seconds.

        A device with no lookups for a site it visits is resolving elsewhere
        (a corporate VPN or web filter such as Zscaler, DoH in the browser),
        so this firewall's blocklist can't be what blocked it.
        """
        ip = await resolve_ip(device)
        end = int(time.time())
        rows = await client.client_queries(ip, end - minutes * 60, end)
        grouped = summary.group_queries(rows, blocked_only, contains)
        return _compact(
            {
                "device": ip,
                "minutes": minutes,
                "queries": len(rows),
                "truncated": len(rows) >= 500,
                "totals": grouped["totals"],
                "domains_total": len(grouped["domains"]),
                "domains": grouped["domains"][:limit],
            }
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def opnsense_client(query: str) -> str:
        """Find a device on the LAN by IP, MAC or (part of a) hostname.

        Joins its Kea DHCP lease (hostname, MAC vendor, dynamic/static,
        active), its Kea reservation and its Unbound host overrides. A device
        with a reservation and an `A` override is pinned: same address and
        `.lan`-style name whatever DHCP does. An IP match is exact; a
        hostname match is a substring, so it can return several devices.
        """
        leases, reservations, overrides = await asyncio.gather(
            client.leases(), client.reservations(), client.host_overrides()
        )
        found = summary.match_devices(query, leases, reservations, overrides)
        if not found:
            raise ToolError(f"no lease, reservation or host override matches {query!r}")
        return _compact(found)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def opnsense_firewall_log(
        address: str | None = None,
        action: str | None = "block",
        dstport: str | None = None,
        scan: int = 5000,
        limit: int = 40,
    ) -> str:
        """Firewall-log entries grouped into flows: rule, action, direction,
        source, destination, protocol and port, with counts, first/last time
        and TCP flags.

        Scans the newest `scan` log lines across all interfaces (OPNsense's
        API can't search further back; raise `scan` up to 50000 for a quiet
        address over a longer period, `oldest` says how far it reached), then
        keeps entries where `address` (an IP, MAC or hostname, resolved
        through DHCP) is source or destination, with `action` (`block`,
        `pass`, or null for both) and destination port `dstport`.

        On a block, `new_connections` counts SYNs. Blocks with only
        `PA`/`FA`/`RA` flags are packets for connections pf no longer tracks
        (after sleep, Wi-Fi roaming or an idle timeout), usually harmless
        noise rather than something being refused.
        """
        ip = await resolve_ip(address) if address else None
        rows = await client.firewall_log(max(1, min(scan, 50000)))
        out = summary.group_firewall(rows, ip, action, dstport)
        out["flows_total"] = len(out["flows"])
        out["flows"] = out["flows"][:limit]
        return _compact(out)

    @server.tool(annotations=PIN, structured_output=False)
    async def opnsense_pin_host(
        mac: str,
        ip: str,
        hostname: str,
        domain: str = "lan",
        description: str = "",
        dry_run: bool = True,
    ) -> str:
        """Pin a device: a Kea DHCP reservation (MAC -> IP) plus an Unbound
        host override (`hostname.domain` -> IP, with PTR), then apply both
        services.

        Dry run by default: shows the subnet the IP falls in and any
        conflicts (the IP, MAC or name already reserved or overridden),
        and changes nothing. With `dry_run=false` and no conflicts it adds
        both entries and reconfigures Kea and Unbound. The device keeps its
        old address until it renews its lease (reconnect it, or wait).
        Pick an IP outside the subnet's dynamic pool.
        """
        if not summary.is_ip(ip):
            raise ToolError(f"{ip!r} is not an IP address")
        if not summary.MAC.match(mac):
            raise ToolError(f"{mac!r} is not a MAC address")
        mac = summary.norm_mac(mac)
        fqdn = f"{hostname}.{domain}"
        subnets, reservations, overrides = await asyncio.gather(
            client.subnets(), client.reservations(), client.host_overrides()
        )
        subnet = summary.subnet_for(ip, subnets)
        if subnet is None:
            raise ToolError(f"{ip} is in no Kea subnet: {[s.get('subnet') for s in subnets]}")
        conflicts = summary.pin_conflicts(ip, mac, fqdn, reservations, overrides)
        plan = {
            "reservation": {
                "subnet": subnet.get("subnet"),
                "mac": mac,
                "ip": ip,
                "hostname": hostname,
            },
            "host_override": {"name": fqdn, "A": ip, "ptr": True},
            "conflicts": conflicts,
        }
        if dry_run or conflicts:
            return _compact({"dry_run": True, **plan})

        res_uuid = _saved(
            await client.add_reservation(
                {
                    "subnet": subnet["uuid"],
                    "ip_address": ip,
                    "hw_address": mac,
                    "hostname": hostname,
                    "description": description,
                }
            ),
            "reservation",
        )
        kea = await client.reconfigure_kea()
        host_uuid = _saved(
            await client.add_host_override(
                {
                    "enabled": "1",
                    "hostname": hostname,
                    "domain": domain,
                    "rr": "A",
                    "server": ip,
                    "addptr": "1",
                    "description": description,
                }
            ),
            "host override",
        )
        unbound = await client.reconfigure_unbound()
        return _compact(
            {
                "dry_run": False,
                **plan,
                "reservation_uuid": res_uuid,
                "host_override_uuid": host_uuid,
                "kea": kea.get("status"),
                "unbound": unbound.get("status"),
            }
        )
