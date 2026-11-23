from jtec_mcp.jellyfin import playback

TV = playback.PROFILES["tv"]


def stream(index, kind, codec, **kw):
    return {"Index": index, "Type": kind, "Codec": codec, **kw}


# A 4K remux shaped like the one that stalled on the TV: 66.9 Mb/s, TrueHD
# default with AC3 alternatives, an embedded default SRT and PGS tracks.
REMUX = {
    "Container": "mkv",
    "Bitrate": 66_913_544,
    "Size": 82_200_613_424,
    "DefaultAudioStreamIndex": None,
    "DefaultSubtitleStreamIndex": None,
    "MediaStreams": [
        stream(0, "Video", "hevc", VideoRangeType="HDR10", Width=3840, Height=2160),
        stream(1, "Audio", "truehd", Language="eng", Channels=8, IsDefault=True),
        stream(2, "Audio", "dts", Language="eng", Channels=8),
        stream(3, "Audio", "ac3", Language="eng", Channels=6),
        stream(5, "Subtitle", "subrip", Language="eng", IsDefault=True, IsTextSubtitleStream=True),
        stream(7, "Subtitle", "PGSSUB", Language="eng"),
    ],
}


def test_remux_transcodes_for_bitrate_and_truehd_and_warns_on_extraction():
    r = playback.check(REMUX, TV)
    assert r["verdict"] == "transcode"
    reasons = {t["reason"]: t["basis"] for t in r["transcode"]}
    assert reasons == {
        "ContainerBitrateExceedsLimit": "observed",
        "AudioCodecNotSupported": "observed",
    }
    assert "82 GB" in r["risks"][0] and "external SRT" in r["risks"][0]
    small = dict(REMUX, Bitrate=8_000_000, Size=2e9)
    assert playback.check(small, TV)["risks"] is None
    assert "audio track 3 (ac3 6ch)" in r["tips"][0]
    assert r["source"]["subtitle_default"]["index"] == 5


def test_direct_play_web_dl():
    src = {
        "Bitrate": 8_000_000,
        "Size": 3e9,
        "MediaStreams": [
            stream(0, "Video", "hevc", VideoRangeType="SDR"),
            stream(1, "Audio", "eac3", Language="eng", IsDefault=True),
            stream(2, "Subtitle", "subrip", IsExternal=True, IsTextSubtitleStream=True),
        ],
    }
    r = playback.check(src, TV)
    assert r["verdict"] == "direct play" and r["transcode"] is None and r["risks"] is None


def test_default_pgs_burns_in_and_dv_only_transcodes():
    src = {
        "Bitrate": 20_000_000,
        "MediaStreams": [
            stream(0, "Video", "hevc", VideoRangeType="DOVI"),
            stream(1, "Audio", "ac3", IsDefault=True),
            stream(2, "Subtitle", "PGSSUB", IsForced=True),
        ],
    }
    reasons = {t["reason"] for t in playback.check(src, TV)["transcode"]}
    assert reasons == {"SubtitleCodecNotSupported", "VideoRangeTypeNotSupported"}
    assert playback.check(src, TV, subtitle_mode="None")["transcode"][0]["reason"] == (
        "VideoRangeTypeNotSupported"
    )


def test_dts_is_a_risk_not_a_verdict():
    src = {
        "Bitrate": 10_000_000,
        "MediaStreams": [
            stream(0, "Video", "h264"),
            stream(1, "Audio", "dts", Language="eng", IsDefault=True),
        ],
    }
    r = playback.check(src, TV)
    assert r["verdict"] == "direct play, with risks" and "dts" in r["risks"][0]


def test_dv_with_base_layer_plays_and_subtitles_are_trimmed():
    streams = [
        stream(0, "Video", "hevc", VideoRangeType="DOVIWithEL"),
        stream(1, "Audio", "eac3", Language="eng", IsDefault=True),
        stream(2, "Subtitle", "PGSSUB", Language="eng"),
        stream(
            3,
            "Subtitle",
            "subrip",
            Language="fra",
            IsForced=True,
            IsExternal=True,
            IsTextSubtitleStream=True,
        ),
    ]
    streams += [stream(10 + i, "Subtitle", "PGSSUB", Language="deu") for i in range(5)]
    r = playback.check({"Bitrate": 30_000_000, "MediaStreams": streams}, TV)
    assert r["verdict"] == "direct play"
    assert [s["index"] for s in r["source"]["subtitles"]] == [2, 3]
    assert r["source"]["other_subtitles"] == 5
