"""Pure helpers: find an album's download jobs and summarise them."""

from __future__ import annotations

# Statuses Aurral's per-job cancel accepts (services/albumJobs.ts). "blocked"
# jobs wait for review and are not cancellable; "cancel_requested" is already
# on its way out.
CANCELLABLE = frozenset({"pending", "downloading"})
ACTIVE = CANCELLABLE | {"cancel_requested", "blocked"}

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def norm_title(text: str | None) -> str:
    """Case, whitespace and curly-quote insensitive form of a title."""
    return " ".join((text or "").translate(_QUOTES).lower().split())


def match_album_jobs(
    jobs: list[dict],
    release_group: str | None = None,
    title: str | None = None,
    artist: str | None = None,
) -> list[dict]:
    """Jobs for one album: same release-group MBID, or the same album title
    (and artist, if given). Title matching catches jobs whose MBID differs or
    is missing, e.g. old request jobs no longer linked to the album's tracks."""
    rg = (release_group or "").lower()
    t, a = norm_title(title), norm_title(artist)
    out = []
    for job in jobs:
        by_mbid = rg and (job.get("albumMbid") or "").lower() == rg
        by_title = (
            t
            and norm_title(job.get("albumName")) == t
            and (not a or norm_title(job.get("artistName")) == a)
        )
        if by_mbid or by_title:
            out.append(job)
    return out


def summarise_job(job: dict) -> dict:
    disc, track = job.get("discNumber"), job.get("trackNumber")
    pos = f"{disc or 1}.{track}" if track is not None else None
    out = {
        "id": job.get("id"),
        "status": job.get("status"),
        "pos": pos,
        "track": job.get("trackName"),
        "album": job.get("albumName"),
        "kind": job.get("requestKind"),
        "reason": job.get("reason"),
        "source": job.get("downloadSource"),
        "peer": job.get("remoteUsername"),
        "error": job.get("error"),
        "final_path": job.get("finalPath"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def order_jobs(jobs: list[dict]) -> list[dict]:
    """Active jobs first, then by disc and track."""
    return sorted(
        jobs,
        key=lambda j: (
            j.get("status") not in ACTIVE,
            j.get("discNumber") or 1,
            j.get("trackNumber") or 0,
        ),
    )
