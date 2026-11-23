from jtec_mcp.slskd.ranking import folders_from_responses, rank


def resp(user, folder, names, ext="flac", free=True, queue=0, speed=1_000_000, **attrs):
    return {
        "username": user,
        "hasFreeUploadSlot": free,
        "queueLength": queue,
        "uploadSpeed": speed,
        "files": [{"filename": f"{folder}\\{n}.{ext}", "size": 1_000_000, **attrs} for n in names]
        + [{"filename": f"{folder}\\cover.jpg", "size": 10}],
    }


TITLES = ["Lone Digger", "Comics", "Mighty"]


def test_groups_by_folder_and_ignores_non_audio():
    r = resp("a", "music\\Album", ["01 Lone Digger", "02 Comics"])
    r["files"] += [{"filename": "music\\Other\\notes.txt", "size": 1}]
    (folder,) = folders_from_responses([r])
    assert folder.path == "music\\Album"
    assert len(folder.audio) == 2
    assert folder.formats == {"flac": 2}


def test_title_matching_ranks_complete_folder_first():
    partial = resp("fast", "x\\Album", ["01 Lone Digger", "02 Comics"], speed=50_000_000)
    complete = resp("slow", "y\\Album", ["01 Lone Digger", "02 Comics", "03 Mighty (feat. JFTH)"])
    ranked = rank(folders_from_responses([partial, complete], TITLES))
    assert [f.username for f in ranked] == ["slow", "fast"]
    assert ranked[0].matched_titles == 3


def test_lossless_beats_lossy_then_slot_queue_speed():
    names = ["01 Lone Digger", "02 Comics", "03 Mighty"]
    mp3 = resp("mp3", "a\\Album", names, ext="mp3", speed=99_000_000)
    busy = resp("busy", "b\\Album", names, free=False)
    queued = resp("queued", "c\\Album", names, queue=5)
    best = resp("best", "d\\Album", names, speed=2_000_000)
    good = resp("good", "e\\Album", names, speed=1_000_000)
    ranked = rank(folders_from_responses([mp3, busy, queued, best, good], TITLES))
    assert [f.username for f in ranked] == ["best", "good", "queued", "busy", "mp3"]


def test_min_tracks_and_summary():
    r = resp("a", "Album", ["01 A", "02 B"], bitDepth=24, sampleRate=88200)
    assert rank(folders_from_responses([r]), min_tracks=3) == []
    s = rank(folders_from_responses([r]))[0].summary(1)
    assert s["tracks"] == 2 and s["bit_depth"] == [24] and s["sample_rate"] == [88200]
    assert s["matched_titles"] is None
