"""The naming policy: pure, deterministic, valid everywhere, and honest about what it will not name."""

from __future__ import annotations

import json
import random
import unicodedata

import pytest

from knowledgevista.domain import naming as n

ONE = json.dumps([{"family": "Examplar", "given": "A.", "name": None}])
TWO = json.dumps([{"family": "Examplar", "given": "A.", "name": None}, {"family": "Placeholder", "given": "B.", "name": None}])
NAME_ONLY = json.dumps([{"family": None, "given": None, "name": "The Invented Consortium"}])


def stem(title="Aqueous Solubility of Invented Esters", authors=ONE, year="2021", ext=".pdf", layout="in_place"):
    return n.name_for(title, authors, year, ext, layout)


def test_the_shape_is_first_author_et_al_year_dash_title_and_the_extension_is_lower_cased():
    assert stem().filename == "Examplar (2021) - Aqueous Solubility of Invented Esters.pdf"
    assert stem(authors=TWO).filename == "Examplar et al. (2021) - Aqueous Solubility of Invented Esters.pdf"
    assert stem(authors=NAME_ONLY).filename == "The Invented Consortium (2021) - Aqueous Solubility of Invented Esters.pdf"
    assert stem(ext=".PDF").extension == ".pdf"


def test_a_lead_author_typed_as_one_string_uses_the_part_before_the_comma():
    typed = json.dumps([{"family": None, "given": None, "name": "Examplar, A."}, {"family": None, "given": None, "name": "Placeholder, B."}])
    assert stem(authors=typed).filename == "Examplar et al. (2021) - Aqueous Solubility of Invented Esters.pdf"
    single = json.dumps([{"family": None, "given": None, "name": "Examplar, A."}])
    assert stem(authors=single).filename == "Examplar (2021) - Aqueous Solubility of Invented Esters.pdf"
    assert stem(authors=json.dumps(["not an object", {"name": "Okay, X."}])).filename.startswith("Okay (2021)")


def test_what_is_unknown_is_left_out_not_invented():
    assert stem(authors=None).filename == "(2021) - Aqueous Solubility of Invented Esters.pdf"
    assert stem(year=None).filename == "Examplar - Aqueous Solubility of Invented Esters.pdf"
    assert stem(authors=None, year=None).filename == "Aqueous Solubility of Invented Esters.pdf"
    assert stem(year="21st").filename == "Examplar - Aqueous Solubility of Invented Esters.pdf"  # not a year: not used
    assert stem(authors="not json").filename == "(2021) - Aqueous Solubility of Invented Esters.pdf"


@pytest.mark.parametrize("title", [None, "", "   ", "?*<>", "\u200b\u202e", "..."])
def test_nothing_is_named_without_an_accepted_title(title):
    assert n.name_for(title, ONE, "2021", ".pdf") is None


def test_the_same_inputs_give_the_same_name_every_time():
    assert {stem().filename for _ in range(5)} == {stem().filename}


def test_the_layout_decides_the_folder_and_in_place_names_no_folder():
    assert stem().subdirectory is None
    assert stem(layout="by_year").subdirectory == "2021"
    assert stem(year=None, layout="by_year").subdirectory == n.UNKNOWN_YEAR_DIR
    with pytest.raises(ValueError, match="unknown layout"):
        stem(layout="by_color")


def test_the_snapshot_records_exactly_what_the_name_was_built_from():
    snapshot = stem(layout="by_year").snapshot
    assert snapshot == {"title": "Aqueous Solubility of Invented Esters", "authors": ONE, "year": "2021", "layout": "by_year"}


# ------------------------------------------------------------------------------------------------ characters


@pytest.mark.parametrize(("title", "expected"), [
    ("Solubility: a study", "Solubility - a study"),
    ("Either/or", "Either - or"), ("a\\b", "a - b"), ("a|b", "a - b"),
    ('Say "hello"', "Say 'hello'"), ("What?", "What"), ("a*b<c>d", "abcd"),
    ("  padded   out  ", "padded out"), ("trailing dots...", "trailing dots"), ("tab\tseparated", "tab separated"),
    ("line\nbreak", "line break"), ("nul\x00byte", "nulbyte"), ("a\u00a0b", "a b"),
])
def test_forbidden_and_awkward_characters_are_replaced_or_dropped(title, expected):
    assert n.clean(title) == expected


def test_unicode_has_one_spelling_and_invisible_tricks_are_removed():
    decomposed, composed = "Cafe\u0301", "Caf\u00e9"
    assert n.clean(decomposed) == composed and n.clean(composed) == composed
    assert n.clean("exe\u202efdp.") == "exefdp"  # a right-to-left override could display this as pdf.exe
    assert n.clean("zero\u200bwidth") == "zerowidth" and n.clean("a\ufeffb") == "ab"
    assert n.clean("\u03b2-lactam \u65e5\u672c\u8a9e") == "\u03b2-lactam \u65e5\u672c\u8a9e"  # real text survives untouched


@pytest.mark.parametrize("bad", ["CON", "con", "PRN", "AUX", "NUL", "COM1", "com9", "LPT3", "COM\u00b9", "CON.v2"])
def test_reserved_device_names_become_ordinary_names_with_or_without_an_extension(bad):
    head, dot, rest = bad.partition(".")
    assert n.avoid_reserved(bad) == head + "_" + dot + rest
    decision = n.name_for(bad, None, None, ".pdf")
    assert decision.filename == head + "_" + dot + rest + ".pdf" and not n.component_problems(decision.filename)


def test_ordinary_names_that_merely_start_like_a_device_are_left_alone():
    for fine in ("CONSOLE", "Communication", "COM10", "NULL", "AUXILIARY"):
        assert n.avoid_reserved(fine) == fine


# ------------------------------------------------------------------------------------------------ length


def test_a_long_stem_is_cut_with_a_stable_tag_of_the_whole_and_two_that_differ_only_past_the_cut_stay_different():
    base = "Very long title " * 20
    one, two = stem(title=base + "ends one").stem, stem(title=base + "ends two").stem
    assert len(one) == len(two) == n.MAX_STEM and one != two and one[-9] == "~" and not one.endswith((" ", "."))
    assert stem(title=base + "ends one").stem == one  # the same cut every time
    assert len(stem(title="x" * n.MAX_STEM).stem) == n.MAX_STEM  # exactly at the limit: untouched, no tag
    assert "~" not in stem(title="x" * 60).stem


def test_shorten_never_returns_more_than_the_limit_whatever_the_limit():
    for limit in (12, 30, 120):
        assert len(n.shorten("a" * 500, limit)) == limit


# ------------------------------------------------------------------------------------------------ collisions


def test_unique_destinations_are_untouched():
    out = n.disambiguate([("i1", "a" * 64, "x/One (2020) - T.pdf"), ("i2", "b" * 64, "x/Two (2020) - T.pdf")])
    assert out == {"i1": "x/One (2020) - T.pdf", "i2": "x/Two (2020) - T.pdf"}


def test_every_member_of_a_collision_gets_its_artifact_tag_and_none_is_favoured():
    entries = [("i1", "a" * 64, "d/Same.pdf"), ("i2", "b" * 64, "d/Same.pdf"), ("i3", "c" * 64, "d/Other.pdf")]
    out = n.disambiguate(entries)
    assert out == {"i1": "d/Same [aaaaaaaa].pdf", "i2": "d/Same [bbbbbbbb].pdf", "i3": "d/Other.pdf"}
    assert n.disambiguate(list(reversed(entries))) == out  # the order the items were listed in does not matter


def test_names_that_differ_only_by_case_or_normalisation_collide_because_the_filesystem_may_think_they_are_one():
    out = n.disambiguate([("i1", "a" * 64, "d/Caf\u00e9.pdf"), ("i2", "b" * 64, "d/cafe\u0301.pdf"), ("i3", "c" * 64, "d/CAF\u00c9.PDF")])
    assert len(set(map(n.collision_key, out.values()))) == 3 and all("[" in v for v in out.values())


def test_one_artifact_wanted_at_one_path_twice_gets_a_counter_in_item_key_order():
    out = n.disambiguate([("loc-b", "a" * 64, "d/Same.pdf"), ("loc-a", "a" * 64, "d/Same.pdf")])
    assert out == {"loc-a": "d/Same [aaaaaaaa].pdf", "loc-b": "d/Same [aaaaaaaa] -2.pdf"}


def test_a_collision_tag_on_a_name_with_no_extension_or_a_dotfile_goes_at_the_end():
    out = n.disambiguate([("i1", "a" * 64, "d/README"), ("i2", "b" * 64, "d/readme"), ("i3", "c" * 64, "d/.hidden"), ("i4", "d" * 64, "d/.HIDDEN")])
    assert out["i1"] == "d/README [aaaaaaaa]" and out["i3"].startswith("d/.hidden [")


def test_disambiguation_is_a_pure_function_of_its_input_under_shuffling():
    rng = random.Random(7)
    entries = [(f"i{k}", f"{k % 4:064x}"[:64], f"d/{'ABab'[k % 2]}.pdf") for k in range(12)]
    expected = n.disambiguate(entries)
    for _ in range(10):
        rng.shuffle(entries)
        assert n.disambiguate(entries) == expected


# ------------------------------------------------------------------------------------------------ destination validation


@pytest.mark.parametrize("path", ["", "/abs.pdf", "a\\b.pdf", "../up.pdf", "a/../b.pdf", "a//b.pdf", "./x.pdf", "C:/x.pdf", "a/b:stream.pdf", "trailing.pdf.", "trailing .pdf ",
                                  "con.pdf", "a/NUL", "bad|name.pdf", 'q"q.pdf', "star*.pdf", "ctl\x01.pdf", "bidi\u202e.pdf", "x" * 201 + ".pdf"])
def test_destinations_a_plan_must_refuse_are_named_with_a_reason(path):
    assert n.component_problems(path), path


@pytest.mark.parametrize("path", ["a.pdf", "dir/sub/Name (2021) - Title.pdf", "caf\u00e9.pdf", "\u65e5\u672c/x.pdf", "1.2.3.pdf", "COM10.pdf", ".hidden"])
def test_ordinary_destinations_pass(path):
    assert n.component_problems(path) == []


def test_everything_the_policy_names_passes_the_validator_for_hostile_input():
    hostile = ["CON", "a:b", "x" * 400, "..", "tab\there", "\u202eevil", "trailing.", " lead", "NUL.txt", "a" * 119 + ".", "?", "ok"]
    for title in hostile:
        decision = n.name_for(title, ONE, "2021", ".pdf", "by_year")
        if decision is None:
            continue
        relative = f"{decision.subdirectory}/{decision.filename}"
        assert n.component_problems(relative) == [], (title, relative)
        assert unicodedata.normalize("NFC", relative) == relative


def test_a_stem_exactly_at_the_limit_is_untouched_and_one_over_is_cut():
    assert n.shorten("a" * n.MAX_STEM) == "a" * n.MAX_STEM
    cut = n.shorten("a" * (n.MAX_STEM + 1))
    assert len(cut) == n.MAX_STEM and cut[-9] == "~"
