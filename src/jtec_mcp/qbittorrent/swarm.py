"""Pure: summarise a torrent and diagnose a stalled swarm from its peer list.

The pattern this looks for (seen with a fake REPACK): hundreds of peers sit at
exactly the same percentage and none has more, while the only "seed" reports
100% but never sends data (qBittorrent flag `d`: we're interested, it chokes
us) and keeps reconnecting. Availability then stays below 1: pieces beyond the
plateau don't exist in the swarm. qBittorrent's "last seen complete" is no
help, because a fake seed refreshes it.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime

STALLED_STATES = {"stalledDL", "metaDL", "forcedMetaDL"}
NO_ETA = 8640000  # qBittorrent's "infinity"


def _iso(ts: int | None) -> str | None:
    if not ts or ts < 0:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(t: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    last = t.get("last_activity")
    eta = t.get("eta")
    return {
        "name": t.get("name"),
        "hash": t.get("hash"),
        "state": t.get("state"),
        "category": t.get("category") or None,
        "progress_pct": round(100 * (t.get("progress") or 0), 1),
        "size_gb": round(size / 1e9, 2) if (size := t.get("size") or 0) > 0 else None,
        "availability": round(t["availability"], 3) if t.get("availability", -1) >= 0 else None,
        "seeds": f"{t.get('num_seeds', 0)} connected / {t.get('num_complete', 0)} in swarm",
        "leechers": f"{t.get('num_leechs', 0)} connected / {t.get('num_incomplete', 0)} in swarm",
        "dl_mb_s": round((t.get("dlspeed") or 0) / 1e6, 2),
        "eta_min": round(eta / 60) if eta is not None and 0 <= eta < NO_ETA else None,
        "added": _iso(t.get("added_on")),
        "last_activity_min_ago": round((now - last) / 60) if last and last > 0 else None,
        "seen_complete": _iso(t.get("seen_complete")),
    }


def is_stalled(t: dict) -> bool:
    return t.get("state") in STALLED_STATES or (
        (t.get("progress") or 0) < 1 and not t.get("dlspeed") and t.get("state") == "downloading"
    )


def diagnose(t: dict, peers: list[dict]) -> dict:
    """Read a swarm's peer list; `findings` says what's wrong in plain words."""
    seeds = [p for p in peers if (p.get("progress") or 0) >= 1]
    others = [p for p in peers if (p.get("progress") or 0) < 1]
    pcts = Counter(int(100 * (p.get("progress") or 0)) for p in others)
    plateau, at_plateau = pcts.most_common(1)[0] if pcts else (None, 0)
    max_other = max(pcts) if pcts else None
    sending = [s for s in seeds if (s.get("dl_speed") or 0) > 0]
    availability = t.get("availability", -1)

    findings = []
    if t.get("state") in ("metaDL", "forcedMetaDL"):
        findings.append(
            "metadata not received yet: it's a magnet and no peer has sent the torrent's "
            "info. A release that relies on web seeds (e.g. Internet Archive) needs the "
            ".torrent file, not a magnet."
        )
    if not peers:
        findings.append("no peers connected")
    elif (
        plateau is not None
        and plateau == max_other
        and at_plateau >= 3
        and at_plateau >= 0.5 * len(others)
        and not sending
    ):
        findings.append(
            f"swarm stuck at {plateau}%: {at_plateau} of {len(others)} downloading peers sit "
            "there and none has more, so the rest of the pieces are probably missing from "
            "the swarm"
        )
    if seeds and not sending:
        clients = sorted({s.get("client") or "?" for s in seeds})
        findings.append(
            f"{len(seeds)} peer(s) report 100% but none is sending "
            f"(clients: {', '.join(clients)}). A seed that never unchokes us and keeps "
            "reconnecting may be fake, e.g. an anti-piracy monitor."
        )
    if 0 <= availability < 1 and peers:
        findings.append(f"availability {availability:.3f}: no full copy among connected peers")
    if not findings:
        findings.append("downloading normally" if t.get("dlspeed") else "no problem found")

    return {
        "peers": len(peers),
        "seeds_connected": len(seeds),
        "seeds_sending": len(sending),
        "commonest_peer_pct": plateau,
        "peers_at_commonest": at_plateau,
        "max_peer_pct": max_other,
        "findings": findings,
    }


def tracker_summary(trackers: list[dict]) -> dict:
    real = [t for t in trackers if not str(t.get("url", "")).startswith("** [")]
    return {
        "trackers": len(real),
        "working": sum(1 for t in real if t.get("status") == 2),
        "failing": sum(1 for t in real if t.get("status") == 4),
        "max_seeds_reported": max((t.get("num_seeds", -1) for t in real), default=-1),
    }
