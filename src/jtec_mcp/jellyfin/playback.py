"""Pure: predict whether a client will direct play a Jellyfin media source.

A client profile lists what the device chain accepts. The default, `tv`, is a
Chromecast with Google TV running Wholphin, feeding a display that does HDR10
and HLG but not Dolby Vision, and a soundbar that only takes AC3/E-AC3
passthrough. Each rule says whether it was `observed` (seen in Jellyfin's own
TranscodeReasons or on the device) or `assumed`, so a prediction never reads
as more certain than it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IMAGE_SUBS = {"pgssub", "hdmv_pgs_subtitle", "dvdsub", "dvd_subtitle", "dvbsub", "dvb_subtitle"}
# Full-file subtitle extraction: on an 82 GB remux it ran ~22 min and stalled
# Wholphin; small files finish in seconds.
SLOW_EXTRACT_BYTES = 20e9


@dataclass(frozen=True)
class Profile:
    name: str
    max_bitrate: int
    video: frozenset[str]
    video_ranges: frozenset[str]
    audio_ok: frozenset[str]
    audio_transcode: frozenset[str]
    notes: tuple[str, ...] = field(default_factory=tuple)


PROFILES = {
    "tv": Profile(
        name="tv: Wholphin on Chromecast with Google TV (40 Mb/s cap), HDR10/HLG display, "
        "AC3/E-AC3 soundbar",
        max_bitrate=40_000_000,
        video=frozenset({"h264", "hevc", "vp9"}),
        video_ranges=frozenset(
            {
                "SDR",
                "Unknown",
                "HDR10",
                "HDR10Plus",
                "HLG",
                "DOVIWithHDR10",
                "DOVIWithHLG",
                "DOVIWithSDR",
            }
        ),
        audio_ok=frozenset({"ac3", "eac3", "aac", "mp3", "opus", "vorbis", "flac"}),
        audio_transcode=frozenset({"truehd"}),
        notes=(
            "Video transcodes are NVENC HEVC tone-mapped to 8-bit SDR.",
            "DTS on this chain is unverified: it may decode on the device or transcode.",
        ),
    ),
}


def _stream(streams: list[dict], kind: str, index: int | None) -> dict | None:
    of_kind = [s for s in streams if s.get("Type") == kind]
    if index is not None and index >= 0:
        for s in of_kind:
            if s.get("Index") == index:
                return s
    for s in of_kind:
        if s.get("IsDefault"):
            return s
    return of_kind[0] if of_kind and kind != "Subtitle" else None


def describe_stream(s: dict) -> dict:
    out = {"index": s.get("Index"), "codec": s.get("Codec"), "lang": s.get("Language")}
    if s.get("Type") == "Video":
        out.update(
            res=f"{s.get('Width')}x{s.get('Height')}",
            range=s.get("VideoRangeType"),
            mbps=round((s.get("BitRate") or 0) / 1e6, 1),
        )
    elif s.get("Type") == "Audio":
        out.update(channels=s.get("Channels"), title=s.get("Title"))
    else:
        out.update(
            title=s.get("Title"),
            external=s.get("IsExternal"),
            image=not s.get("IsTextSubtitleStream"),
        )
    if s.get("IsDefault"):
        out["default"] = True
    if s.get("IsForced"):
        out["forced"] = True
    return {k: v for k, v in out.items() if v not in (None, False)}


def check(source: dict, profile: Profile, subtitle_mode: str = "Default") -> dict:
    """Predict playback of one media source (an item's MediaSources[n])."""
    streams = source.get("MediaStreams") or []
    video = _stream(streams, "Video", None)
    audio = _stream(streams, "Audio", source.get("DefaultAudioStreamIndex"))
    sub = _stream(streams, "Subtitle", source.get("DefaultSubtitleStreamIndex"))
    if sub is None and subtitle_mode in ("Default", "Always", "Smart"):
        sub = next(
            (
                s
                for s in streams
                if s.get("Type") == "Subtitle" and (s.get("IsDefault") or s.get("IsForced"))
            ),
            None,
        )
    if subtitle_mode == "None":
        sub = None

    transcode, risks, tips = [], [], []
    bitrate = source.get("Bitrate") or 0
    if bitrate > profile.max_bitrate:
        transcode.append(
            {
                "reason": "ContainerBitrateExceedsLimit",
                "detail": f"{bitrate / 1e6:.1f} Mb/s > {profile.max_bitrate / 1e6:.0f} Mb/s cap",
                "basis": "observed",
            }
        )
    if video:
        codec = (video.get("Codec") or "").lower()
        if codec not in profile.video:
            transcode.append(
                {"reason": "VideoCodecNotSupported", "detail": codec, "basis": "assumed"}
            )
        rng = video.get("VideoRangeType") or "SDR"
        if rng not in profile.video_ranges:
            transcode.append(
                {
                    "reason": "VideoRangeTypeNotSupported",
                    "detail": f"{rng} (no HDR10 fallback for this display)",
                    "basis": "assumed",
                }
            )
    if audio:
        codec = (audio.get("Codec") or "").lower()
        if codec in profile.audio_transcode:
            transcode.append(
                {"reason": "AudioCodecNotSupported", "detail": codec, "basis": "observed"}
            )
        elif codec not in profile.audio_ok:
            risks.append(f"default audio {codec}: support on this client unverified")
        if codec not in profile.audio_ok:
            alt = [
                s
                for s in streams
                if s.get("Type") == "Audio"
                and (s.get("Codec") or "").lower() in {"eac3", "ac3"}
                and s.get("Language") == audio.get("Language")
            ]
            if alt:
                best = max(alt, key=lambda s: (s.get("Codec") == "eac3", s.get("Channels") or 0))
                tips.append(
                    f"audio track {best.get('Index')} ({best.get('Codec')} "
                    f"{best.get('Channels')}ch) passes through; selecting it, or making it the "
                    "default, avoids the audio transcode"
                )
    if sub:
        codec = (sub.get("Codec") or "").lower()
        if codec in IMAGE_SUBS:
            transcode.append(
                {
                    "reason": "SubtitleCodecNotSupported",
                    "detail": f"{codec} subtitle {sub.get('Index')} is burned in",
                    "basis": "observed",
                }
            )
        elif not sub.get("IsExternal") and (size := source.get("Size") or 0) > SLOW_EXTRACT_BYTES:
            # Small files extract in seconds; only big ones stall the client.
            risks.append(
                f"embedded text subtitle {sub.get('Index')} is on by default: the first time, "
                f"Jellyfin reads the whole {size / 1e9:.0f} GB file to extract subtitles, which "
                "takes minutes and stalls Wholphin meanwhile. An external SRT avoids it."
            )

    return {
        "verdict": "transcode"
        if transcode
        else ("direct play, with risks" if risks else "direct play"),
        "transcode": transcode or None,
        "risks": risks or None,
        "tips": tips or None,
        "source": {
            "container": source.get("Container"),
            "mbps": round(bitrate / 1e6, 1),
            "size_gb": round((source.get("Size") or 0) / 1e9, 1),
            "video": describe_stream(video) if video else None,
            "audio_default": describe_stream(audio) if audio else None,
            "subtitle_default": describe_stream(sub) if sub else None,
            "audio_tracks": [describe_stream(s) for s in streams if s.get("Type") == "Audio"],
            "subtitles": [describe_stream(s) for s in streams if s.get("Type") == "Subtitle"],
        },
    }
