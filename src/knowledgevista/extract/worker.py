"""The extraction worker: a separate process that reads PDFs for the supervisor in `client.py`.

WHY A PROCESS. A PDF is untrusted input and PyMuPDF is native code. A hostile or merely pathological file can hang the
parser or exhaust memory; in-process that takes the scan, the GUI or the MCP server down with it. Here the worst case is
that this process is killed and the supervisor records a failed page.

PROTOCOL (one JSON object per line, ASCII-escaped, on stdout; requests arrive on stdin):

    -> {"cmd": "extract", "path": "...", "start": 1}
    <- {"type": "open", "pages": N, "repaired": bool, "has_labels": bool}      or
       {"type": "open_error", "error": "..."}
    <- {"type": "page", "n": 1, "text": "...", "label": "iv" | null, "images": 0}      one per page, in order
       {"type": "page", "n": 7, "error": "..."}                                        a page that failed alone
    <- {"type": "end"}

    -> {"cmd": "front", "path": "..."}
    <- {"type": "front", "pages": N, "info": {...}, "xmp": "...", "lines": [{"p","t","s","b","x","y"}], "page_size": [w, h]}  or
       {"type": "open_error", "error": "..."}

stdout is the protocol and nothing else: anything a library prints is redirected to stderr first, so a stray
`print` can never be mistaken for a page. The process serves many files and is replaced by the supervisor on
a schedule or after a failure, so a slow leak cannot accumulate.
"""

from __future__ import annotations

import gc
import json
import sys


def _emit(stream, message: dict) -> None:
    stream.write(json.dumps(message, ensure_ascii=True) + "\n")
    stream.flush()


def main() -> int:
    protocol = sys.stdout
    sys.stdout = sys.stderr  # nothing but the protocol may reach the real stdout
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if request.get("cmd") == "exit":
            return 0
        if request.get("cmd") == "extract":
            _serve(pymupdf, protocol, request["path"], int(request.get("start", 1)))
        elif request.get("cmd") == "front":
            _front(pymupdf, protocol, request["path"])
        else:
            continue
        gc.collect()
    return 0


#: What the front-matter request returns, bounded so a hostile file cannot make the reply large.
FRONT_PAGES = 2
MAX_FRONT_LINES = 150
MAX_LINE_CHARS = 300
MAX_XMP_CHARS = 65536
MAX_INFO_CHARS = 1000


def _front(pymupdf, protocol, path: str) -> None:
    """PDF Info, XMP and the line layout (text, font size, boldness, position) of the first two pages.

    Reported as untrusted: this is what the file CLAIMS and where it puts its biggest text, which
    `domain/frontmatter.py` weighs against the page text. One reply, no streaming: it is bounded by construction.
    """
    try:
        document = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - any parser failure is reported, never allowed to end the worker
        _emit(protocol, {"type": "open_error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return
    try:
        if document.needs_pass:
            _emit(protocol, {"type": "open_error", "error": "encrypted: the file needs a password"})
            return
        if document.page_count == 0:
            _emit(protocol, {"type": "open_error", "error": "no pages could be read (damaged or empty)"})
            return
        try:
            info = {key: str(value)[:MAX_INFO_CHARS] for key, value in (document.metadata or {}).items()
                    if key in ("title", "author", "subject", "keywords") and value}
        except Exception:  # noqa: BLE001 - a damaged Info dictionary costs the Info, not the layout
            info = {}
        try:
            xmp = (document.get_xml_metadata() or "")[:MAX_XMP_CHARS]
        except Exception:  # noqa: BLE001
            xmp = ""
        lines: list[dict] = []
        size = None
        for index in range(min(FRONT_PAGES, document.page_count)):
            try:
                page = document[index]
                if size is None:
                    size = [round(page.rect.width, 1), round(page.rect.height, 1)]
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                        if not spans or len(lines) >= MAX_FRONT_LINES * FRONT_PAGES:
                            continue
                        text = "".join(s["text"] for s in line["spans"]).strip()
                        weight = sum(len(s["text"]) for s in spans)
                        dominant = max(spans, key=lambda s: len(s["text"]))
                        bold = sum(len(s["text"]) for s in spans if s.get("flags", 0) & 16 or "bold" in s.get("font", "").lower()) * 2 > weight
                        lines.append({"p": index + 1, "t": text[:MAX_LINE_CHARS], "s": round(float(dominant["size"]), 1), "b": bold,
                                      "x": round(line["bbox"][0], 1), "y": round(line["bbox"][1], 1)})
            except Exception:  # noqa: BLE001 - one bad page must not cost the other
                continue
        _emit(protocol, {"type": "front", "pages": document.page_count, "info": info, "xmp": xmp, "lines": lines, "page_size": size})
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001, S110 - closing a damaged document may itself fail; nothing to do about it
            pass


def _serve(pymupdf, protocol, path: str, start: int) -> None:
    try:
        document = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - any parser failure is reported, never allowed to end the worker
        _emit(protocol, {"type": "open_error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return
    try:
        if document.needs_pass:
            _emit(protocol, {"type": "open_error", "error": "encrypted: the file needs a password"})
            return
        count = document.page_count
        if count == 0:
            _emit(protocol, {"type": "open_error", "error": "no pages could be read (damaged or empty)"})
            return
        _emit(protocol, {"type": "open", "pages": count, "repaired": bool(document.is_repaired),
                         "has_labels": bool(document.get_page_labels())})
        for index in range(max(0, start - 1), count):
            try:
                page = document[index]
                text = page.get_text()
                # An image count is only needed to tell a scan from a blank page, so only ask when there is no text.
                images = len(page.get_images()) if not text.strip() else 0
                _emit(protocol, {"type": "page", "n": index + 1, "text": text, "label": page.get_label() or None, "images": images})
            except Exception as exc:  # noqa: BLE001 - one bad page must not cost the others
                _emit(protocol, {"type": "page", "n": index + 1, "error": f"{type(exc).__name__}: {exc}"[:300]})
        _emit(protocol, {"type": "end"})
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001, S110 - closing a damaged document may itself fail; nothing to do about it
            pass


if __name__ == "__main__":
    raise SystemExit(main())
