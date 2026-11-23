"""Thin async client for the Servarr apps (Sonarr, Radarr, Lidarr, Prowlarr,
Whisparr, and Readarr-compatible Chaptarr). They share one API shape; what
differs per app (API version, the library resource, queue include flags, the
history route) is in APPS. Each app publishes an OpenAPI spec in its source
tree (src/<App>.Api.V<n>/openapi.json); this client covers the few endpoints
the tools use."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError, check


@dataclass(frozen=True)
class App:
    api: str
    library: str | None = None  # resource listing the library: series, movie, ...
    title_field: str = "title"
    history: str | None = None  # history/<x>?<x>Id=
    queue_includes: tuple[str, ...] = ()


APPS: dict[str, App] = {
    "sonarr": App(
        "v3",
        "series",
        history="series",
        queue_includes=("includeSeries", "includeEpisode", "includeUnknownSeriesItems"),
    ),
    "radarr": App(
        "v3",
        "movie",
        history="movie",
        queue_includes=("includeMovie", "includeUnknownMovieItems"),
    ),
    # Whisparr v3 is Radarr-based: movies, not scenes.
    "whisparr": App(
        "v3",
        "movie",
        history="movie",
        queue_includes=("includeMovie", "includeUnknownMovieItems"),
    ),
    "lidarr": App(
        "v1",
        "artist",
        "artistName",
        history="artist",
        queue_includes=("includeArtist", "includeAlbum", "includeUnknownArtistItems"),
    ),
    # Chaptarr speaks Readarr's API. Its /book list is too big to fetch
    # (it timed out at 30 s for ~1,000 books), so the library is its authors.
    "chaptarr": App(
        "v1",
        "author",
        "authorName",
        history="author",
        queue_includes=("includeAuthor", "includeBook", "includeUnknownAuthorItems"),
    ),
    "prowlarr": App("v1"),
}


class ArrClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if service.name not in APPS:
            raise ToolError(f"unknown Servarr app {service.name!r}; known: {', '.join(APPS)}")
        self.name = service.name
        self.app = APPS[service.name]
        self.label = service.name.capitalize()
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api/{self.app.api}",
            headers={"X-Api-Key": api_key},
            verify=service.verify_tls,
            timeout=60,
            transport=transport,
        )

    @property
    def has_queue(self) -> bool:
        return self.app.library is not None

    async def get(self, path: str, **params):
        r = check(await self._http.get(path, params=params or None), self.label)
        return r.json() if r.content else None

    async def status(self) -> dict:
        return await self.get("/system/status")

    async def health(self) -> list[dict]:
        return await self.get("/health")

    async def queue_status(self) -> dict:
        return await self.get("/queue/status")

    async def root_folders(self) -> list[dict]:
        return await self.get("/rootfolder")

    async def queue(self, page_size: int = 200) -> list[dict]:
        params = {"pageSize": page_size, **{k: "true" for k in self.app.queue_includes}}
        data = await self.get("/queue", **params)
        return data.get("records", []) if isinstance(data, dict) else data or []

    async def library(self) -> list[dict]:
        return await self.get(f"/{self.app.library}")

    async def quality_profiles(self) -> dict[int, str]:
        return {p["id"]: p["name"] for p in await self.get("/qualityprofile")}

    async def history(self, item_id: int) -> list[dict]:
        h = self.app.history
        params = {f"{h}Id": item_id}
        if self.name == "sonarr":
            params["includeEpisode"] = "true"
        data = await self.get(f"/history/{h}", **params)
        return data.get("records", []) if isinstance(data, dict) else data or []
