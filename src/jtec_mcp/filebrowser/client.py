"""Thin async client for FileBrowser Quantum (gtsteffaniak/filebrowser): list,
search, stat and upload files across its sources. Quantum serves its OpenAPI
spec at /swagger/doc.json (to logged-in users); this covers the endpoints the
tools use.

Pitfalls (v2.1.0):
- Auth is a long-lived API token as `Authorization: Bearer`. Creating one
  (`POST /api/auth/token`) doesn't return it; read it back with
  `GET /api/auth/token?name=`. A token acts as the user who made it, so make
  it as a non-admin user scoped to what the agent may touch.
- `POST /api/resources` answers 409 when the file exists, unless
  `override=true`. It creates missing parent folders.
- `size` is the disk usage (4 KiB blocks) unless the source sets
  `useLogicalSize`; compare files by `checksum=sha256` (returned as
  `checksums.sha256`), never by size.
- `/api/tools/search` needs `sources` (comma-separated); there's no
  "all sources" default. Result paths have no leading slash, and folders end
  in "/".
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import ToolError, check

CHUNK = 1024 * 1024


def norm(path: str) -> str:
    """A source-relative path as the API wants it: leading slash, no trailing."""
    return "/" + str(path).strip("/")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            yield block


class FilebrowserClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        opts = service.options
        self.options = opts
        # The drop folder chaptarr_import_file uploads to.
        self.source = opts.get("source", "default")
        self.upload_dir = norm(opts.get("upload_dir", "/"))
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api",
            headers={"Authorization": f"Bearer {api_key}"},
            verify=service.verify_tls,
            # Large uploads over the LAN; the read side is quick.
            timeout=httpx.Timeout(30, write=600),
            transport=transport,
        )

    def upload_path(self, name: str) -> str:
        return str(PurePosixPath(self.upload_dir) / name)

    async def sources(self) -> dict[str, dict]:
        """Source name -> status, counts and usage (only those the user can see)."""
        r = await self._http.get("/settings/sources")
        return check(r, "FileBrowser").json()

    async def resource(self, source: str, path: str, checksum: bool = False) -> dict | None:
        """A folder (with `folders` and `files`) or a file's details; None if
        it doesn't exist."""
        params = {"source": source, "path": norm(path)}
        if checksum:
            params["checksum"] = "sha256"
        r = await self._http.get("/resources", params=params)
        if r.status_code == 404:
            return None
        return check(r, "FileBrowser").json()

    async def search(
        self,
        query: str,
        sources: list[str],
        limit: int,
        newer_than: int | None = None,
        wildcard: bool = False,
    ) -> list[dict]:
        params: dict = {"query": query, "sources": ",".join(sources), "limit": limit}
        if newer_than is not None:
            params["newerThan"] = newer_than
        if wildcard:
            params["useWildcard"] = "true"
        r = await self._http.get("/tools/search", params=params)
        return check(r, "FileBrowser").json() or []

    async def sha256(self, path: str, source: str | None = None) -> str | None:
        """The file's SHA-256, or None if it doesn't exist."""
        info = await self.resource(source or self.source, path, checksum=True)
        if info is None:
            return None
        return (info.get("checksums") or {}).get("sha256")

    async def upload(self, path: str, local: Path, source: str | None = None) -> None:
        """Stream a local file to `path`; never overwrites."""
        params = {"source": source or self.source, "path": norm(path), "override": "false"}
        r = await self._http.post(
            "/resources",
            params=params,
            content=_chunks(local),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(local.stat().st_size),
            },
        )
        check(r, "FileBrowser")

    async def put(self, local: Path, path: str, source: str | None = None) -> str:
        """Upload unless the same file is already there: "uploaded" or
        "already there". A different file at `path` is a ToolError."""
        existing = await self.sha256(path, source)
        if existing is None:
            await self.upload(path, local, source)
            return "uploaded"
        if existing == file_sha256(local):
            return "already there"
        raise ToolError(
            f"{norm(path)} already exists with different contents; "
            "rename the file or remove the old one"
        )
