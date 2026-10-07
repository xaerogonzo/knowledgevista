# What "search" means

Written down so that nobody changes it by accident. In chemistry a "helpful" correction changes the compound, so every
rule below is deliberate and has a test (`tests/test_extract_service.py`, the query set at the bottom).

## What is searched

The text of PDF pages as extracted by PyMuPDF (`page.get_text()`), one full-text row per page. Nothing else: not
metadata, not file names, not non-PDF files, not images. A page with no text has no row.

Text is **extracted**, not read. Tables arrive as running text, columns can interleave, and equations are lost. A hit tells
you which page to open, never what a value is. The page in the PDF is the authority.

## How a query is read

| You type | It means |
|---|---|
| `aqueous solubility` | the two words next to each other, in that order (a phrase) |
| `solub*` | an explicit prefix: `solubility`, `soluble`, `solubilised`. A bare `solub` matches nothing |
| `PGDN \| propylene glycol dinitrate` | either (alternatives for synonyms and abbreviations) |
| `--near T` | the phrase within `--within` words (default 30) of `T` |
| `--also T` | `T` anywhere on the same page |
| `--file S` | only files whose path contains `S` |

Every term is quoted before it reaches the engine, so `2,4-DNT`, `NEAR(a b)`, `x AND y`, `-negated` and `col:umn` are
searched as text and never parsed as operators. The only operator a term can carry is the trailing `*`.

An empty query is `KV_QUERY_INVALID` (exit 2), never "no results". The query language is versioned
(`query_language_version`, currently 1) so a saved search cannot silently change meaning.

## How text is split into words

SQLite FTS5 with `unicode61 remove_diacritics 2` (the same tokenizer as the OpenChem index, kept for parity):

- case-insensitive;
- accents are folded: `etude` finds `Étude`;
- every punctuation character separates words, so `2,4-DNT` is the phrase `2 4 dnt`, `Cu(II)` is `cu ii`, and
  `10.1021/acs.x` is several words;
- **no stemming** (`soluble` does not find `solubility`) and **no typo correction**;
- **no transliteration**: `β` is not `beta`. A search for `beta-lactam` does not find `β-lactam`. Opt-in
  chemistry-aware normalisation is a deferred idea (`docs/design-notes.md`), not a default.

These consequences are tested as negative cases: `2,4-DNP` does not match `2,4-DNT`, `Cu(III)` does not match
`Cu(II)`, `Al2O` does not match `Al2O3`, `pH 7.40` does not match `pH 7.4`.

## What a result says

Results are ordered by relevance, ties broken by artifact then page, so the same query returns the same list every time.
Each hit carries `anchor = {artifact_id, pdf_page}`, the durable citation. `pdf_page` counts from 1 and is the physical
position; `printed_label` is the number printed on the page, a string, and may be absent. The internal page id is not part
of the contract: it changes when text is re-extracted.

The `snippet` is a navigation aid (the match marked `[[like this]]`), not a quotation.

## What "no results" means

Every search reports its **coverage**: how many PDFs exist, how many were searchable, and how many could not be:

| Field | Meaning |
|---|---|
| `not_yet_extracted` | no text has been read yet; run `kv extract` |
| `no_text_layer` | extracted, but no page has any text (a scan); can never match a text query |
| `partially_indexed` | some pages failed; the rest are searchable |
| `extraction_failed` | the document could not be read at all |
| `provisional_imported` | text imported from the OpenChem index; searchable, replaced by `kv extract --rebuild-imported` |
| `other_files_not_searchable` | non-PDF files, which are not extracted in this version |

If any of the first four is non-zero the response carries a `KV_SEARCH_COVERAGE` warning: **a missing hit is not proof of
absence.** A search over zero searchable documents is the error `KV_NOTHING_SEARCHABLE`, not an empty success.
