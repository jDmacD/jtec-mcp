"""chaptarr_set_book_monitored: monitor or unmonitor single books. Chaptarr
has no author editor (it monitors ebooks and audiobooks separately), so
arr_set_monitored doesn't cover it; this works per book instead, e.g. to stop
Chaptarr searching for a book that reached the library another way."""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer

from jtec_mcp.arr.actions import MONITOR, _compact
from jtec_mcp.arr.client import ArrClient
from jtec_mcp.errors import ToolError


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def pick_author(authors: list[dict], name: str) -> dict:
    """The author whose name is `name`, else the only one containing it."""
    want = _norm(name)
    exact = [a for a in authors if _norm(a.get("authorName") or "") == want]
    if exact:
        return exact[0]
    partial = [a for a in authors if want in _norm(a.get("authorName") or "")]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ToolError(f"Chaptarr has no author matching {name!r}")
    names = ", ".join(sorted(a.get("authorName") or "?" for a in partial)[:10])
    raise ToolError(f"{name!r} matches several authors: {names}")


def register(server: MCPServer, chaptarr: ArrClient) -> None:
    @server.tool(annotations=MONITOR, structured_output=False)
    async def chaptarr_set_book_monitored(
        author: str,
        monitored: bool,
        title: str = "",
        book_ids: list[int] | None = None,
        media_type: Literal["any", "ebook", "audiobook"] = "any",
        dry_run: bool = True,
    ) -> str:
        """Monitor or unmonitor books in Chaptarr.

        Finds `author` (exact name, or a unique part of one), then picks
        their books whose title contains `title`, or the given `book_ids`.
        Chaptarr keeps an ebook and an audiobook record per title;
        `media_type` narrows to one. With neither `title` nor `book_ids`,
        lists the author's monitored books (an author can have 100+ records)
        and changes nothing. Dry run by default: shows each book's id, title,
        media type, monitored flag and whether it has a file. The author's
        own monitoring is left alone, so new books still arrive.
        """
        authors = await chaptarr.library()
        a = pick_author(authors, author)
        books = await chaptarr.get("/book", authorId=a["id"]) or []
        rows = [
            {
                "id": b.get("id"),
                "title": b.get("title"),
                "media_type": b.get("mediaType"),
                "monitored": b.get("monitored"),
                "has_file": bool((b.get("statistics") or {}).get("bookFileCount")),
            }
            for b in sorted(books, key=lambda b: (b.get("title") or "").casefold())
            if media_type == "any" or b.get("mediaType") == media_type
        ]
        out: dict = {"author": {"id": a["id"], "name": a.get("authorName")}}
        if not title and not book_ids:
            watched = [r for r in rows if r["monitored"]]
            return _compact({**out, "books": len(rows), "monitored_books": watched})
        if book_ids:
            chosen = [r for r in rows if r["id"] in set(book_ids)]
            missing = set(book_ids) - {r["id"] for r in chosen}
            if missing:
                raise ToolError(f"{a.get('authorName')} has no books {sorted(missing)}")
        else:
            chosen = [r for r in rows if _norm(title) in _norm(r["title"] or "")]
            if not chosen:
                raise ToolError(f"{a.get('authorName')} has no book matching {title!r}")
        change = [r for r in chosen if bool(r["monitored"]) != monitored]
        out |= {"monitored": monitored, "books": chosen, "changing": [r["id"] for r in change]}
        if dry_run or not change:
            return _compact({"dry_run": dry_run, **out})
        await chaptarr.send(
            "PUT",
            "/book/monitor",
            json={"bookIds": [r["id"] for r in change], "monitored": monitored},
        )
        return _compact(out)
