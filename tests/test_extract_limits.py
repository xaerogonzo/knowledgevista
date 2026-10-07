"""The memory cap: measured on the real mechanism, not assumed (this is the milestone 2 spike, kept as a regression test)."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from knowledgevista.extract import limits

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")

ALLOCATE = "import sys; mb = int(sys.argv[1]); b = bytearray(mb * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); print('allocated', mb, flush=True)"


def run_child(want_mb: int, cap_mb: int):
    process = subprocess.Popen([sys.executable, "-c", ALLOCATE, str(want_mb)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, **limits.popen_kwargs(cap_mb))
    limiter = limits.attach(process, cap_mb)
    out, err = process.communicate(timeout=60)
    return process.returncode, out, err, limiter


def test_allocation_under_the_cap_succeeds():
    code, out, _err, limiter = run_child(want_mb=50, cap_mb=300)
    assert (code, out.strip()) == (0, "allocated 50") and limiter.enforced
    limiter.close()


def test_allocation_over_the_cap_fails_inside_the_child():
    code, out, err, limiter = run_child(want_mb=900, cap_mb=300)
    assert code != 0 and out == "" and "MemoryError" in err, "the child must be refused the memory"
    assert limiter.enforced and limiter.mechanism == "windows-job-object"
    limiter.close()


def test_the_same_allocation_without_a_cap_succeeds_so_the_cap_is_what_stopped_it():
    process = subprocess.Popen([sys.executable, "-c", ALLOCATE, "900"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, _err = process.communicate(timeout=60)
    assert process.returncode == 0 and out.strip() == "allocated 900", "the oracle must be alive: this machine can allocate 900 MB"


def test_closing_the_limiter_kills_a_surviving_child():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **limits.popen_kwargs(300))
    limiter = limits.attach(process, 300)
    time.sleep(0.3)
    assert process.poll() is None
    limiter.close()
    deadline = time.monotonic() + 5
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is not None, "KILL_ON_JOB_CLOSE: a worker must not outlive the supervisor"


def test_attaching_to_a_process_that_already_exited_reports_not_enforced_instead_of_raising():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    limiter = limits.attach(process, 300)
    assert limiter.enforced is False and limiter.mechanism == "none" and "timeout" in limiter.note
    limiter.close()


def test_closing_twice_is_harmless():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], **limits.popen_kwargs(300))
    limiter = limits.attach(process, 300)
    limiter.close()
    limiter.close()
    process.wait(timeout=10)
