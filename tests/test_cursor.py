"""Pagination cursors: continue exactly or refuse, never a page that might skip or repeat."""

from __future__ import annotations

import base64

import pytest

from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import cursor as c


def issue(**kw) -> str:
    base = dict(command="search", signature="sig", revision=7, offset=20, shown=20, last_key="k20", more=True)
    base.update(kw)
    return c.next_cursor(**base)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(KvError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value.message
    return caught.value


def test_a_cursor_round_trips_and_continues_where_the_page_ended():
    token = issue()
    cursor = c.decode(token)
    assert (cursor.command, cursor.signature, cursor.revision, cursor.offset, cursor.after) == ("search", "sig", 7, 40, "k20")
    assert c.start_offset(token, command="search", signature="sig", revision=7) == 40


def test_no_cursor_means_the_start_or_the_explicit_offset():
    assert c.start_offset(None, command="x", signature="s", revision=1) == 0
    assert c.start_offset(None, command="x", signature="s", revision=1, fallback_offset=30) == 30
    refused(ErrorCode.INVALID_ARGUMENTS, c.start_offset, None, command="x", signature="s", revision=1, fallback_offset=-1)
    refused(ErrorCode.INVALID_ARGUMENTS, c.start_offset, None, command="x", signature="s", revision=1, fallback_offset=c.MAX_WINDOW + 1)


def test_a_cursor_from_before_a_catalog_change_is_stale_and_says_what_to_do():
    error = refused(ErrorCode.CURSOR_STALE, c.start_offset, issue(revision=7), command="search", signature="sig", revision=8)
    assert error.details == {"cursor_revision": 7, "catalog_revision": 8} and "without --cursor" in error.message


def test_a_cursor_from_another_command_or_other_arguments_is_a_mistake_not_staleness():
    token = issue()
    refused(ErrorCode.INVALID_ARGUMENTS, c.start_offset, token, command="relations list", signature="sig", revision=7)
    refused(ErrorCode.INVALID_ARGUMENTS, c.start_offset, token, command="search", signature="other", revision=7)


@pytest.mark.parametrize("token", ["", "x", "!!!notbase64!!!", base64.urlsafe_b64encode(b"nodot").decode(), base64.urlsafe_b64encode(b"deadbeef.{}").decode(),
                                   base64.urlsafe_b64encode(b"12345678.not json").decode()])
def test_a_damaged_or_invented_cursor_is_an_argument_error(token):
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, token)


def test_an_edited_cursor_fails_its_checksum():
    token = issue()
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    tampered = base64.urlsafe_b64encode(raw.replace('"o":40', '"o":41').encode()).decode().rstrip("=")
    assert tampered != token
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, tampered)


def test_a_cursor_from_another_cursor_version_or_with_a_bad_offset_is_refused():
    def forge(body: str) -> str:
        text = f"{c._checksum(body)}.{body}"
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    good = '{"a":null,"c":"x","o":1,"r":1,"s":"s","v":%d}'
    assert c.decode(forge(good % 1)).offset == 1
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, forge(good % 2))
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, forge('{"a":null,"c":"x","o":-1,"r":1,"s":"s","v":1}'))
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, forge('{"a":null,"c":"x","o":99999,"r":1,"s":"s","v":1}'))
    refused(ErrorCode.INVALID_ARGUMENTS, c.decode, forge('{"c":"x","v":1}'))


def test_the_item_before_the_page_must_still_be_the_last_one_returned():
    keys = [f"k{i}" for i in range(60)]
    cursor = c.decode(issue())  # offset 40, after k20?  the key list says item 39 is k39: this cursor was for a listing where it was k20
    refused(ErrorCode.CURSOR_STALE, c.check_anchor, cursor, keys)
    good = c.decode(issue(last_key="k39"))
    c.check_anchor(good, keys)  # offset 40, keys[39] == "k39"
    refused(ErrorCode.CURSOR_STALE, c.check_anchor, good, keys[:39])  # the listing shrank below the offset
    c.check_anchor(c.Cursor("x", "s", 1, 0, None), [])  # the first page has nothing before it


def test_start_offset_with_keys_runs_the_anchor_check_too():
    keys = [f"k{i}" for i in range(60)]
    token = issue(last_key="k39")
    assert c.start_offset(token, command="search", signature="sig", revision=7, keys=keys) == 40
    refused(ErrorCode.CURSOR_STALE, c.start_offset, token, command="search", signature="sig", revision=7, keys=["z"] * 60)


def test_the_last_page_has_no_next_cursor_and_neither_does_an_empty_one_or_one_past_the_window():
    assert issue(more=False) is None
    assert issue(shown=0) is None
    assert issue(offset=c.MAX_WINDOW - 5, shown=10) is None
    assert issue(offset=c.MAX_WINDOW - 20, shown=20) is not None


def test_limits_are_refused_not_clamped():
    assert c.bounded_limit(None, 25) == 25 and c.bounded_limit(1, 25) == 1 and c.bounded_limit(c.MAX_LIMIT, 25) == c.MAX_LIMIT
    for bad in (0, -1, c.MAX_LIMIT + 1):
        error = refused(ErrorCode.INVALID_ARGUMENTS, c.bounded_limit, bad, 25)
        assert error.details == {"limit": bad, "maximum": c.MAX_LIMIT}


def test_a_signature_depends_on_the_arguments_and_nothing_else():
    assert c.signature_of("a", 1, None) == c.signature_of("a", 1, None)
    assert c.signature_of("a", 1) != c.signature_of("a", 2) and c.signature_of(["a", "b"]) != c.signature_of(["b", "a"])
    assert len(c.signature_of("x")) == 16
