"""Pure helpers for the OPNsense tools: matching devices across leases,
reservations and host overrides, and grouping query and firewall logs."""

from __future__ import annotations

import ipaddress
import re
from collections import Counter

MAC = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def norm_mac(s: str) -> str:
    h = re.sub(r"[^0-9a-f]", "", s.lower())
    return ":".join(h[i : i + 2] for i in range(0, 12, 2))


def is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
    except ValueError:
        return False
    return True


def match_devices(
    query: str, leases: list[dict], reservations: list[dict], overrides: list[dict]
) -> list[dict]:
    """Devices whose IP or MAC equals `query`, or whose hostname contains it.
    Each device joins its lease, its Kea reservation and its Unbound host
    overrides (matched by IP)."""
    q = query.strip().lower()
    by_mac = MAC.match(q) is not None
    mac_q = norm_mac(q) if by_mac else None

    def hit(ip: str | None, mac: str | None, *names: str | None) -> bool:
        if is_ip(q):
            return ip == q
        if mac_q:
            return bool(mac) and norm_mac(mac) == mac_q
        return any(n and q in n.lower() for n in names)

    devices: dict[str, dict] = {}

    def dev(key: str) -> dict:
        return devices.setdefault(
            key, {"ip": None, "mac": None, "lease": None, "reservation": None, "dns": []}
        )

    for lease in leases:
        ip, mac = lease.get("address"), lease.get("hwaddr")
        if hit(ip, mac, lease.get("hostname")):
            d = dev(mac or ip)
            d.update(ip=ip, mac=mac)
            d["lease"] = {
                "hostname": lease.get("hostname") or None,
                "vendor": lease.get("mac_info") or None,
                "type": lease.get("lease_type"),
                "active": lease.get("state") in (0, "0"),
                "expires": lease.get("expire"),
            }
    for r in reservations:
        ip, mac = r.get("ip_address"), r.get("hw_address")
        if hit(ip, mac, r.get("hostname"), r.get("description")):
            d = dev(mac or ip)
            d["ip"] = d["ip"] or ip
            d["mac"] = d["mac"] or mac
            d["reservation"] = {
                "uuid": r.get("uuid"),
                "ip": ip,
                "hostname": r.get("hostname") or None,
                "description": r.get("description") or None,
            }
    ips = {d["ip"] for d in devices.values() if d["ip"]}
    for o in overrides:
        server = o.get("server")
        fqdn = ".".join(p for p in (o.get("hostname"), o.get("domain")) if p)
        entry = {
            "uuid": o.get("uuid"),
            "name": fqdn,
            "rr": o.get("rr"),
            "value": server,
            "enabled": o.get("enabled") in ("1", 1, True),
            "description": o.get("description") or None,
        }
        if server in ips:
            for d in devices.values():
                if d["ip"] == server:
                    d["dns"].append(entry)
        elif hit(server, None, fqdn, o.get("description")):
            d = dev(server or fqdn)
            d["ip"] = d["ip"] or server
            d["dns"].append(entry)
    return sorted(devices.values(), key=lambda d: d["ip"] or "")


def group_queries(rows: list[dict], blocked_only: bool, contains: str | None) -> dict:
    """Group one client's query log by domain: count, action, blocklist,
    rcodes and last time seen. Blocked domains first, then by count."""
    groups: dict[str, dict] = {}
    actions = Counter()
    for r in rows:
        action = r.get("action")
        actions[action] += 1
        domain = (r.get("domain") or "").rstrip(".")
        if blocked_only and action not in ("Block", "Drop"):
            continue
        if contains and contains.lower() not in domain.lower():
            continue
        g = groups.setdefault(
            domain,
            {
                "domain": domain,
                "count": 0,
                "action": action,
                "blocklist": None,
                "rcodes": Counter(),
                "last": 0,
            },
        )
        g["count"] += 1
        if action in ("Block", "Drop"):
            g["action"] = action
            g["blocklist"] = r.get("blocklist") or g["blocklist"]
        g["rcodes"][r.get("rcode")] += 1
        g["last"] = max(g["last"], int(r.get("time") or 0))
    out = []
    for g in groups.values():
        g["rcodes"] = dict(g["rcodes"])
        out.append(g)
    out.sort(key=lambda g: (g["action"] not in ("Block", "Drop"), -g["count"]))
    return {"totals": dict(actions), "domains": out}


def group_firewall(rows: list[dict], ip: str | None, action: str | None, port: str | None) -> dict:
    """Filter firewall-log entries by address (source or destination), action
    and destination port, then group by rule, direction, peer and port, with
    the TCP flags seen. Flags without `S` on a block are packets of a
    connection pf no longer tracks (state expired or lost), not new
    connections being refused."""
    groups: dict[tuple, dict] = {}
    span = [None, None]
    matched = 0
    for r in rows:
        if ip and ip not in (r.get("src"), r.get("dst")):
            continue
        if action and r.get("action") != action:
            continue
        if port and str(r.get("dstport")) != str(port):
            continue
        matched += 1
        ts = r.get("__timestamp__")
        span[0] = min(span[0] or ts, ts)
        span[1] = max(span[1] or ts, ts)
        key = (
            r.get("label") or r.get("rid"),
            r.get("action"),
            r.get("interface"),
            r.get("dir"),
            r.get("src"),
            r.get("dst"),
            r.get("protoname"),
            r.get("dstport"),
        )
        g = groups.setdefault(
            key,
            {
                "rule": key[0],
                "action": key[1],
                "interface": key[2],
                "dir": key[3],
                "src": key[4],
                "dst": key[5],
                "proto": key[6],
                "dstport": key[7],
                "count": 0,
                "tcpflags": Counter(),
                "first": ts,
                "last": ts,
            },
        )
        g["count"] += 1
        if r.get("tcpflags"):
            g["tcpflags"][r["tcpflags"]] += 1
        g["first"] = min(g["first"], ts)
        g["last"] = max(g["last"], ts)
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["count"]):
        flags = g.pop("tcpflags")
        if flags:
            g["tcpflags"] = dict(flags)
            if g["action"] == "block":
                g["new_connections"] = sum(n for f, n in flags.items() if "S" in f and "A" not in f)
        out.append(g)
    return {
        "matched": matched,
        "scanned": len(rows),
        "oldest": span[0],
        "newest": span[1],
        "flows": out,
    }


def subnet_for(ip: str, subnets: list[dict]) -> dict | None:
    addr = ipaddress.ip_address(ip)
    for s in subnets:
        try:
            if addr in ipaddress.ip_network(s.get("subnet", ""), strict=False):
                return s
        except ValueError:
            continue
    return None


def pin_conflicts(
    ip: str, mac: str, fqdn: str, reservations: list[dict], overrides: list[dict]
) -> list[str]:
    out = []
    for r in reservations:
        if r.get("ip_address") == ip:
            out.append(f"reservation {r.get('uuid')} already holds {ip} ({r.get('hw_address')})")
        if r.get("hw_address") and norm_mac(r["hw_address"]) == norm_mac(mac):
            out.append(f"reservation {r.get('uuid')} already pins {mac} to {r.get('ip_address')}")
    for o in overrides:
        name = ".".join(p for p in (o.get("hostname"), o.get("domain")) if p)
        if name.lower() == fqdn.lower() and o.get("rr") in ("A", None):
            out.append(f"host override {o.get('uuid')} already maps {fqdn} to {o.get('server')}")
    return out
