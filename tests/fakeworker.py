"""A scriptable stand-in for the extraction worker, speaking the same protocol, so the supervisor's failure handling
can be tested against REAL misbehaving processes (a real hang, a real crash, a real memory failure).

Behaviour comes from the KV_FAKE environment variable (JSON):

    {"pages": 10,                    pages per file
     "hang_page": 4,                 stop responding while producing this page (until killed)
     "crash_page": 6,                os._exit(3) while producing this page
     "hang_all": true,               every page hangs (a worker that is poisoned throughout)
     "alloc_mb_page": [5, 900],      allocate this many MB while producing page 5 (to hit a memory cap)
     "garbage": true,                print non-protocol lines before and between the real events
     "error_page": 2,                report page 2 as failed inside the worker (the worker survives)
     "slow_page_s": 0.3,             sleep this long on every page
     "poison": "bad",                a file whose path contains this exits at once, before sending "open"
     "open_error": "boom"}           answer every file with open_error

A crash or hang happens only on the first request for a file (start == 1): after the supervisor restarts the worker
from the next page, the poison page is behind it, which is exactly how a real poison page behaves.
"""

from __future__ import annotations

import json
import os
import sys
import time


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    config = json.loads(os.environ.get("KV_FAKE", "{}"))
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("cmd") == "exit":
            return
        path, start = request["path"], request["start"]
        if config.get("poison") and config["poison"] in path:
            os._exit(9)
        if config.get("garbage"):
            print("some library printed this", flush=True)
            print("{not json", flush=True)
        if config.get("open_error"):
            emit({"type": "open_error", "error": config["open_error"]})
            continue
        count = config.get("pages", 10)
        emit({"type": "open", "pages": count, "repaired": False, "has_labels": False})
        for n in range(start, count + 1):
            if config.get("slow_page_s"):
                time.sleep(config["slow_page_s"])
            if config.get("hang_page") == n or config.get("hang_all"):
                time.sleep(3600)
            if config.get("crash_page") == n:
                sys.stderr.write("fatal: simulated crash in the PDF engine\n")
                sys.stderr.flush()
                os._exit(3)
            alloc = config.get("alloc_mb_page")
            if alloc and alloc[0] == n:
                hold = bytearray(alloc[1] * 1024 * 1024)  # raises MemoryError under a cap; the traceback goes to stderr
                hold[::4096] = b"x" * len(hold[::4096])
            if config.get("garbage") and n % 2 == 0:
                print("noise between pages", flush=True)
            if config.get("error_page") == n:
                emit({"type": "page", "n": n, "error": "RuntimeError: simulated page failure"})
            else:
                emit({"type": "page", "n": n, "text": f"fake page {n} of {os.path.basename(path)} pid {os.getpid()}", "label": None, "images": 0})
        emit({"type": "end"})


if __name__ == "__main__":
    main()
