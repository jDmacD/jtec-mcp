"""files_* tools: browse, search and drop files into FileBrowser Quantum's
sources. What the agent may see and write is whatever the API token's user
may; files_sources says which sources accept uploads."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.errors import ToolError
from jtec_mcp.filebrowser.client import FilebrowserClient, norm
from jtec_mcp.slskd.tools import _compact

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _entry(item: dict, *, source: str | None = None, path: str | None = None) -> dict:
    out = {
        "source": source,
        "path": path,
        "name": None if path else item.get("name"),
        "type": "dir" if item.get("type") == "directory" else item.get("type"),
        "size": item.get("size"),
        "modified": (item.get("modified") or "")[:19] or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def search_results(raw: list[dict], kind: str) -> list[dict]:
    """Search hits as {source, path, type, size, modified}, folders first."""
    out = []
    for hit in raw:
        p = hit.get("path") or ""
        is_dir = p.endswith("/") or hit.get("type") == "directory"
        if (kind == "files" and is_dir) or (kind == "dirs" and not is_dir):
            continue
        out.append(
            _entry(
                {**hit, "type": "directory" if is_dir else hit.get("type")},
                source=hit.get("source"),
                path=norm(p) + ("/" if is_dir else ""),
            )
        )
    out.sort(key=lambda e: (e["type"] != "dir", e.get("source", ""), e["path"].lower()))
    return out


def register(server: MCPServer, fb: FilebrowserClient) -> None:
    async def source_names() -> list[str]:
        return sorted(await fb.sources())

    @server.tool(annotations=READ, structured_output=False)
    async def files_sources() -> str:
        """FileBrowser's sources (top-level folders of the file store): each
        one's index status, folder and file counts, bytes used, and whether
        it accepts uploads (files_upload). Use the names as `source` in the
        other files_* tools."""
        out = []
        for name, s in sorted((await fb.sources()).items()):
            out.append(
                {
                    "name": name,
                    "status": s.get("status"),
                    "writable": not s.get("readOnly", False),
                    "dirs": s.get("numDirs"),
                    "files": s.get("numFiles"),
                    "bytes": s.get("used"),
                }
            )
        return _compact(out)

    @server.tool(annotations=READ, structured_output=False)
    async def files_search(
        query: str,
        sources: list[str] | None = None,
        kind: Literal["any", "files", "dirs"] = "any",
        newer_than_days: float | None = None,
        wildcard: bool = False,
        limit: int = 50,
    ) -> str:
        """Search file and folder names across FileBrowser's index.

        `query` matches names (case-insensitive, words anywhere); with
        `wildcard=true` it's a glob on the name instead, e.g. "*.epub".
        `sources` limits the search (default: all); `kind` keeps only files
        or only folders; `newer_than_days` keeps things modified recently.
        Results are folders first, then by source and path; paths are
        relative to the source (pass them to files_list). The index is
        rebuilt on a schedule, so files added in the last few minutes may be
        missing; files_list reads the disk directly.
        """
        if not query.strip():
            raise ToolError("query is empty")
        names = sources or await source_names()
        newer = int(time.time() - newer_than_days * 86400) if newer_than_days else None
        limit = max(1, min(limit, 500))
        raw = await fb.search(query, names, limit, newer, wildcard)
        results = search_results(raw, kind)
        return _compact(
            {"count": len(results), "maybe_truncated": len(raw) >= limit, "results": results}
        )

    @server.tool(annotations=READ, structured_output=False)
    async def files_list(
        source: str, path: str = "/", limit: int = 200, checksum: bool = False
    ) -> str:
        """List a folder in a FileBrowser source, or show one file.

        A folder gives its subfolders then its files, each with size (bytes)
        and modified time, up to `limit` entries. A file gives its size, type
        and modified time, plus its SHA-256 with `checksum=true` (slow for
        large files). Reads the disk, so it's current even when the search
        index isn't.
        """
        info = await fb.resource(source, path, checksum=checksum)
        if info is None:
            raise ToolError(f"{source}:{norm(path)} doesn't exist")
        if info.get("type") != "directory":
            out = _entry(info, source=source, path=norm(path))
            if sha := (info.get("checksums") or {}).get("sha256"):
                out["sha256"] = sha
            return _compact(out)
        dirs = sorted(info.get("folders") or [], key=lambda d: d.get("name", "").lower())
        files = sorted(info.get("files") or [], key=lambda f: f.get("name", "").lower())
        entries = [_entry(d) for d in dirs] + [_entry(f) for f in files]
        return _compact(
            {
                "source": source,
                "path": norm(path),
                "bytes": info.get("size"),
                "dirs": len(dirs),
                "files": len(files),
                "entries": entries[: max(1, limit)],
                "truncated": len(entries) > limit,
            }
        )

    @server.tool(annotations=WRITE, structured_output=False)
    async def files_upload(file: str, source: str, folder: str = "/", name: str = "") -> str:
        """Upload a local file from this host into a FileBrowser source.

        Puts `file` (a local path) at `folder`/`name` in `source` (`name`
        defaults to the file's own name); missing folders are created. Never
        overwrites: if the same file (by SHA-256) is already there it's left
        alone, and a different file with that name is an error. Only sources
        files_sources lists as writable accept uploads, and even there the
        token's user may be limited to some folders. For books meant for
        Chaptarr, use chaptarr_import_file instead.
        """
        local = Path(file).expanduser()
        if not local.is_file():
            raise ToolError(f"{local} is not a file on this host")
        dest = str(PurePosixPath(norm(folder)) / (name or local.name))
        result = await fb.put(local, dest, source)
        return _compact(
            {"source": source, "path": dest, "bytes": local.stat().st_size, "upload": result}
        )
