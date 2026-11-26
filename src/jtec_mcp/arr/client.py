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
    # Item-level search command: (command name, body field, field takes a list)
    search: tuple[str, str, bool] | None = None
    # Bulk monitoring editor: (path, ids field); None where unsupported
    editor: tuple[str, str] | None = None


APPS: dict[str, App] = {
    "sonarr": App(
        "v3",
        "series",
        history="series",
        queue_includes=("includeSeries", "includeEpisode", "includeUnknownSeriesItems"),
        search=("SeriesSearch", "seriesId", False),
        editor=("/series/editor", "seriesIds"),
    ),
    "radarr": App(
        "v3",
        "movie",
        history="movie",
        queue_includes=("includeMovie", "includeUnknownMovieItems"),
        search=("MoviesSearch", "movieIds", True),
        editor=("/movie/editor", "movieIds"),
    ),
    # Whisparr v3 is Radarr-based: movies, not scenes.
    "whisparr": App(
        "v3",
        "movie",
        history="movie",
        queue_includes=("includeMovie", "includeUnknownMovieItems"),
        search=("MoviesSearch", "movieIds", True),
        editor=("/movie/editor", "movieIds"),
    ),
    "lidarr": App(
        "v1",
        "artist",
        "artistName",
        history="artist",
        queue_includes=("includeArtist", "includeAlbum", "includeUnknownArtistItems"),
        search=("ArtistSearch", "artistId", False),
        editor=("/artist/editor", "artistIds"),
    ),
    # Chaptarr speaks Readarr's API. Its /book list is too big to fetch
    # (it timed out at 30 s for ~1,000 books), so the library is its authors.
    "chaptarr": App(
        "v1",
        "author",
        "authorName",
        history="author",
        queue_includes=("includeAuthor", "includeBook", "includeUnknownAuthorItems"),
        # No editor: Chaptarr monitors ebooks and audiobooks separately.
        search=("AuthorSearch", "authorId", False),
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

    async def send(
        self, method: str, path: str, params: dict | None = None, json=None, timeout=None
    ):
        kw = {"timeout": timeout} if timeout else {}
        r = await self._http.request(method, path, params=params, json=json, **kw)
        r = check(r, self.label)
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

    async def queue_remove(
        self, queue_id: int, remove_from_client: bool, blocklist: bool, search: bool
    ) -> None:
        params = {
            "removeFromClient": str(remove_from_client).lower(),
            "blocklist": str(blocklist).lower(),
            "skipRedownload": str(not search).lower(),
        }
        await self.send("DELETE", f"/queue/{queue_id}", params=params)

    async def command(self, name: str, **body) -> dict:
        return await self.send("POST", "/command", json={"name": name, **body})

    async def command_status(self, command_id: int) -> dict:
        return await self.get(f"/command/{command_id}")

    async def manual_import(self, folder: str) -> list[dict]:
        """What the app would import from `folder`: one candidate per file,
        with its matched item, quality and rejections. Changes nothing."""
        return await self.get("/manualimport", folder=folder, filterExistingFiles="false")

    async def episodes(self, series_id: int) -> list[dict]:
        return await self.get("/episode", seriesId=series_id)

    async def set_monitored(self, ids: list[int], monitored: bool) -> None:
        path, field = self.app.editor
        body = {field: ids, "monitored": monitored}
        if self.name == "lidarr" and not monitored:
            body["monitorNewItems"] = "none"
        await self.send("PUT", path, json=body)

    async def albums(self) -> list[dict]:
        return await self.get("/album")

    async def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        await self.send(
            "PUT", "/album/monitor", json={"albumIds": album_ids, "monitored": monitored}
        )

    # Prowlarr
    async def indexers(self) -> list[dict]:
        return await self.get("/indexer")

    async def indexer_status(self) -> list[dict]:
        return await self.get("/indexerstatus")

    async def tags(self) -> dict[int, str]:
        return {t["id"]: t["label"] for t in await self.get("/tag")}
