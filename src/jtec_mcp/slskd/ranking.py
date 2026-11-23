"""Turn raw slskd search responses into ranked album folders.

A search returns files per peer. For whole-album downloads what matters is the
folder: one peer's copy of one edition. Folders are ranked by completeness,
then lossless before lossy, then how soon the peer will actually serve them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

AUDIO_EXTENSIONS = {
    "flac",
    "mp3",
    "m4a",
    "aac",
    "ogg",
    "opus",
    "wav",
    "aiff",
    "aif",
    "alac",
    "ape",
    "wv",
}
LOSSLESS_EXTENSIONS = {"flac", "wav", "aiff", "aif", "alac", "ape", "wv"}


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _split(filename: str) -> tuple[str, str]:
    """Soulseek paths use backslashes regardless of the peer's OS."""
    folder, _, name = filename.rpartition("\\")
    return folder, name


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class Folder:
    username: str
    path: str
    files: list[dict] = field(default_factory=list)  # every file, as slskd returned it
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0  # bytes/s
    matched_titles: int | None = None

    @property
    def audio(self) -> list[dict]:
        return [f for f in self.files if _ext(f["filename"]) in AUDIO_EXTENSIONS]

    @property
    def formats(self) -> dict[str, int]:
        return dict(Counter(_ext(f["filename"]) for f in self.audio))

    @property
    def lossless(self) -> bool:
        return bool(self.audio) and all(
            _ext(f["filename"]) in LOSSLESS_EXTENSIONS for f in self.audio
        )

    @property
    def total_bytes(self) -> int:
        return sum(f.get("size", 0) for f in self.files)

    def _distinct(self, key: str) -> list:
        return sorted({f[key] for f in self.audio if f.get(key)})

    def summary(self, n: int) -> dict:
        return {
            "n": n,
            "user": self.username,
            "folder": self.path,
            "tracks": len(self.audio),
            "matched_titles": self.matched_titles,
            "formats": self.formats,
            "bit_depth": self._distinct("bitDepth"),
            "sample_rate": self._distinct("sampleRate"),
            "bitrate": self._distinct("bitRate"),
            "size_mb": round(self.total_bytes / 1e6, 1),
            "free_slot": self.has_free_slot,
            "queue": self.queue_length,
            "speed_mb_s": round(self.upload_speed / 1e6, 2),
        }


def folders_from_responses(
    responses: list[dict], track_titles: list[str] | None = None
) -> list[Folder]:
    titles = [_norm(t) for t in track_titles or [] if _norm(t)]
    folders: list[Folder] = []
    for r in responses:
        by_path: dict[str, Folder] = {}
        for f in r.get("files", []):
            path, _ = _split(f["filename"])
            folder = by_path.get(path)
            if folder is None:
                folder = by_path[path] = Folder(
                    username=r["username"],
                    path=path,
                    has_free_slot=bool(r.get("hasFreeUploadSlot")),
                    queue_length=int(r.get("queueLength") or 0),
                    upload_speed=int(r.get("uploadSpeed") or 0),
                )
            folder.files.append(f)
        for folder in by_path.values():
            if not folder.audio:
                continue
            if titles:
                names = [_norm(_split(f["filename"])[1]) for f in folder.audio]
                folder.matched_titles = sum(any(t in n for n in names) for t in titles)
            folders.append(folder)
    return folders


def rank(folders: list[Folder], min_tracks: int = 1) -> list[Folder]:
    def key(f: Folder):
        completeness = f.matched_titles if f.matched_titles is not None else len(f.audio)
        return (-completeness, not f.lossless, not f.has_free_slot, f.queue_length, -f.upload_speed)

    return sorted((f for f in folders if len(f.audio) >= min_tracks), key=key)
