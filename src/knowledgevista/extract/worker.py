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
        if request.get("cmd") != "extract":
            continue
        _serve(pymupdf, protocol, request["path"], int(request.get("start", 1)))
        gc.collect()
    return 0


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
