"""Thin async client for FileBrowser Quantum (gtsteffaniak/filebrowser), used
as an HTTP drop folder: stat and upload files in one source. Quantum serves
its OpenAPI spec at /swagger (for API-enabled users); this covers the two
endpoints the tools use.

Pitfalls (v2.1.0):
- Auth is a long-lived API token as `Authorization: Bearer`. Creating one
  (`POST /api/auth/token`) doesn't return it; read it back with
  `GET /api/auth/token?name=`.
- `POST /api/resources` answers 409 when the file exists, unless
  `override=true`.
- `size` in a stat is rounded up to whole 4 KiB blocks, not the byte size;
  compare files by `checksum=sha256` (returned as `checksums.sha256`).
"""

from __future__ import annotations

from pathlib import PurePosixPath

import httpx

from jtec_mcp.config import ServiceConfig
from jtec_mcp.errors import check


class FilebrowserClient:
    def __init__(
        self,
        service: ServiceConfig,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        opts = service.options
        self.options = opts
        self.source = opts.get("source", "default")
        # Where uploads go inside the source, e.g. "/downloads/incoming".
        self.upload_dir = "/" + str(opts.get("upload_dir", "/")).strip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{service.url}/api",
            headers={"Authorization": f"Bearer {api_key}"},
            verify=service.verify_tls,
            # Uploads of large books over the LAN; the read side is quick.
            timeout=httpx.Timeout(30, write=600),
            transport=transport,
        )

    def upload_path(self, name: str) -> str:
        return str(PurePosixPath(self.upload_dir) / name)

    async def sha256(self, path: str) -> str | None:
        """The file's SHA-256, or None if it doesn't exist."""
        params = {"source": self.source, "path": path, "checksum": "sha256"}
        r = await self._http.get("/resources", params=params)
        if r.status_code == 404:
            return None
        return (check(r, "FileBrowser").json().get("checksums") or {}).get("sha256")

    async def upload(self, path: str, data: bytes, override: bool = False) -> None:
        params = {"source": self.source, "path": path, "override": str(override).lower()}
        r = await self._http.post(
            "/resources",
            params=params,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        check(r, "FileBrowser")
