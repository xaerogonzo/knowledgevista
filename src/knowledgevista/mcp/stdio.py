"""The stdio transport: one JSON-RPC message per line on stdin, one reply per line on stdout, nothing else on stdout ever.

Diagnostics go to stderr. The loop ends when the client closes stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from knowledgevista.mcp.protocol import Server
from knowledgevista.mcp.tools import ReadContext


def serve(catalog: Path, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source, sink = stdin or sys.stdin, stdout or sys.stdout
    if hasattr(source, "reconfigure"):
        source.reconfigure(encoding="utf-8")
    if hasattr(sink, "reconfigure"):
        sink.reconfigure(encoding="utf-8", newline="\n")  # replies are single lines ending in LF on every platform
    server = Server(ReadContext(catalog))
    for line in source:
        reply = server.handle_line(line)
        if reply is not None:
            sink.write(reply + "\n")
            sink.flush()
    return 0
