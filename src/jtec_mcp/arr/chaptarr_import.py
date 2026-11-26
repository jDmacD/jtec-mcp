"""chaptarr_import_file: hand Chaptarr a book file from this host, through a
FileBrowser drop folder that Chaptarr can also see, and import it with
Chaptarr's manual import. Registered when both Chaptarr and FileBrowser are
configured.

The FileBrowser options say where uploads land and where Chaptarr sees the
same folder:

    [services.filebrowser]
    source = "books"
    upload_dir = "/downloads/incoming"
    chaptarr_dir = "/data/books/downloads/incoming"
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jtec_mcp.arr.actions import _compact, _wait_command
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.errors import ToolError
from jtec_mcp.filebrowser.client import FilebrowserClient

log = logging.getLogger("jtec_mcp")

IMPORT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
# Ebooks are a few MB; refuse anything that is plainly not one book.
MAX_BYTES = 2 * 1024**3


def _match(candidate: dict) -> dict:
    """The parts of a manual-import candidate worth showing."""
    author, book, edition = (candidate.get(k) or {} for k in ("author", "book", "edition"))
    quality = (candidate.get("quality") or {}).get("quality") or {}
    out = {
        "author": {"id": author.get("id"), "name": author.get("authorName")} if author else None,
        "book": (
            {
                "id": book.get("id"),
                "title": book.get("title"),
                "media_type": book.get("mediaType"),
                "has_files": book.get("hasFiles"),
            }
            if book
            else None
        ),
        "edition": (
            {
                "id": edition.get("id"),
                "isbn13": edition.get("isbn13"),
                "publisher": edition.get("publisher"),
                "format": edition.get("format"),
            }
            if edition
            else None
        ),
        "quality": quality.get("name"),
        "rejections": [r.get("reason") for r in candidate.get("rejections") or []] or None,
        "warnings": candidate.get("warnings") or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def _problems(candidate: dict) -> list[str]:
    problems = [r.get("reason") or str(r) for r in candidate.get("rejections") or []]
    for key in ("author", "book"):
        if not (candidate.get(key) or {}).get("id"):
            problems.append(f"no {key} matched")
    if not (candidate.get("editionId") or (candidate.get("edition") or {}).get("id")):
        problems.append("no edition matched")
    if not candidate.get("quality"):
        problems.append("no quality detected")
    return problems


def _file_entry(candidate: dict) -> dict:
    """One ManualImport file, as Chaptarr's UI builds it for an automatic match."""
    return {
        "path": candidate["path"],
        "authorId": candidate["author"]["id"],
        "bookId": candidate["book"]["id"],
        "editionId": candidate.get("editionId") or candidate["edition"]["id"],
        "selectionSource": "automatic",
        "quality": candidate["quality"],
        "releaseGroup": candidate.get("releaseGroup"),
        "indexerFlags": candidate.get("indexerFlags", 0),
        "downloadId": candidate.get("downloadId"),
        "disableReleaseSwitching": candidate.get("disableReleaseSwitching", False),
    }


def register(server: MCPServer, chaptarr: ArrClient, fb: FilebrowserClient) -> None:
    chaptarr_dir = fb.options.get("chaptarr_dir")
    if not chaptarr_dir:
        log.warning("services.filebrowser has no chaptarr_dir; chaptarr_import_file not registered")
        return
    chaptarr_dir = "/" + str(chaptarr_dir).strip("/")

    @server.tool(annotations=IMPORT, structured_output=False)
    async def chaptarr_import_file(file: str, dry_run: bool = True, wait_s: int = 60) -> str:
        """Import a book file from this host into Chaptarr.

        Uploads `file` (a local path, e.g. a zlib download) to the FileBrowser
        drop folder Chaptarr also sees, then runs Chaptarr's manual-import
        matching on it and shows the match: author, book, edition, quality and
        any rejections. Upload is skipped when the same file (by SHA-256) is
        already there; a different file with the same name is an error
        (rename it).

        Dry run by default: the file is uploaded and matched but not imported.
        With `dry_run=false` and a clean match (no rejections, author, book and
        edition all matched), Chaptarr imports it (move) and its import hook
        passes it on as usual; waits up to `wait_s` for the command. Unmatched
        or rejected files are left in the drop folder; fix the book in
        Chaptarr (or import by hand in its UI) and try again.
        """
        path = Path(file).expanduser()
        if not path.is_file():
            raise ToolError(f"{path} is not a file on this host")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ToolError(f"{path.name} is {size} bytes; that isn't one book")

        uploaded = await fb.put(path, fb.upload_path(path.name))

        seen = str(PurePosixPath(chaptarr_dir) / path.name)
        candidates = await chaptarr.manual_import(chaptarr_dir)
        candidate = next((c for c in candidates if c.get("path") == seen), None)
        if candidate is None:
            raise ToolError(
                f"Chaptarr doesn't list {seen} in a manual import of {chaptarr_dir}; "
                "check that chaptarr_dir maps to FileBrowser's upload_dir"
            )
        out = {"file": seen, "upload": uploaded, "match": _match(candidate)}
        problems = _problems(candidate)
        if problems:
            out["problems"] = problems
        if dry_run or problems:
            out["imported"] = False
            out["would_import"] = not problems
            return _compact(out)

        cmd = await chaptarr.command(
            "ManualImport",
            files=[_file_entry(candidate)],
            importMode="move",
            replaceExistingFiles=False,
        )
        out["command"] = await _wait_command(chaptarr, cmd, wait_s)
        out["imported"] = out["command"]["status"] == "completed"
        return _compact(out)
