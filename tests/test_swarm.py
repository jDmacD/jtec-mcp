from jtec_mcp.qbittorrent import swarm

STALLED = {
    "hash": "ab" * 20,
    "name": "Film 2026 REPACK 1080p",
    "state": "stalledDL",
    "progress": 0.642,
    "availability": 0.642,
    "num_seeds": 1,
    "num_complete": 2,
    "num_leechs": 400,
    "num_incomplete": 506,
    "dlspeed": 0,
    "size": 3_760_000_000,
    "eta": 8640000,
    "added_on": 1_700_000_000,
    "last_activity": 1_700_000_000,
    "seen_complete": 1_700_000_500,
}


def test_fake_seed_and_plateau_are_reported():
    peers = [{"progress": 0.64, "dl_speed": 0, "flags": "d"} for _ in range(10)]
    peers += [{"progress": 0.2, "dl_speed": 0}, {"progress": 0.01, "dl_speed": 0}]
    peers.append({"progress": 1.0, "dl_speed": 0, "flags": "d U X", "client": "Transmission 2.21"})
    d = swarm.diagnose(STALLED, peers)
    assert d["seeds_connected"] == 1 and d["seeds_sending"] == 0
    assert d["commonest_peer_pct"] == 64 and d["peers_at_commonest"] == 10
    text = " ".join(d["findings"])
    assert "stuck at 64%" in text
    assert "Transmission 2.21" in text and "never unchokes" in text
    assert "availability 0.642" in text


def test_healthy_download_and_no_peers():
    ok = dict(STALLED, state="downloading", dlspeed=10_000_000, availability=3.5)
    peers = [{"progress": 1.0, "dl_speed": 5_000_000}, {"progress": 0.3}]
    assert swarm.diagnose(ok, peers)["findings"] == ["downloading normally"]
    assert "no peers connected" in swarm.diagnose(STALLED, [])["findings"]


def test_magnet_without_metadata():
    meta = dict(STALLED, state="metaDL", progress=0, availability=0)
    assert any("metadata not received" in f for f in swarm.diagnose(meta, [])["findings"])


def test_summary_units_and_infinite_eta():
    s = swarm.summarize(STALLED, now=1_700_000_600)
    assert s["progress_pct"] == 64.2 and s["size_gb"] == 3.76
    assert s["eta_min"] is None and s["last_activity_min_ago"] == 10
    assert s["seeds"] == "1 connected / 2 in swarm"
    assert swarm.is_stalled(STALLED)
    assert not swarm.is_stalled(dict(STALLED, state="uploading", progress=1))


def test_tracker_summary_skips_dht_pex_lsd():
    t = [
        {"url": "** [DHT] **", "status": 2, "num_seeds": 50},
        {"url": "udp://a", "status": 2, "num_seeds": 4},
        {"url": "udp://b", "status": 4, "num_seeds": -1},
    ]
    assert swarm.tracker_summary(t) == {
        "trackers": 2,
        "working": 1,
        "failing": 1,
        "max_seeds_reported": 4,
    }


def test_unknown_size_is_none():
    assert swarm.summarize(dict(STALLED, size=-1))["size_gb"] is None
