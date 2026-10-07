# Relations, duplicates and virtual organisation

Milestone 4. This page is the policy; the code is `services/relations.py` (the store, merge and split), `services/relate.py`
(the detectors), `domain/relation_evidence.py` (the pure evidence functions), `services/organize.py` (collections, tags,
saved searches and views) and `cli_relations.py` (the commands).

## The one rule

**A relation is either a proposal or an accepted fact, and the two live in different tables**, exactly as a metadata value
does (docs/METADATA.md). `kv relate` writes proposals (`relation_candidate`). A relation (`artifact_relation`,
`document_relation`) exists only after a person accepts a proposal or states it with `kv relations add`. Nothing is merged,
related or grouped by a run. A proposal is keyed by its evidence, so a rerun that finds the same evidence changes nothing; a
proposal whose evidence has gone is marked `stale` (never deleted); a decision, accepted or rejected, is never reopened.

## Two levels, and merging is a third thing

| Level | Kinds | About |
|---|---|---|
| artifact | `duplicate_of`, `derivative_of`, `replaces`, `equivalent_to` | bytes: this file is a copy, derivative or replacement of that one |
| document | `supplement_of`, `part_of`, `version_of`, `related_to` | library items: this is the supplement of that paper, this chapter is part of that book |
| (decision) | `same_document` | these two documents are ONE item; accepting it merges them |
| (group) | `collection` | many documents belong together under a name (a book that is not itself in the library) |

Symmetric kinds (`equivalent_to`, `same_document`, `related_to`) are stored smaller id first, so one fact has one row. The
hierarchical kinds (`part_of`, `supplement_of`, `version_of`) refuse a cycle: a document cannot be its own ancestor.

## The four levels of "the same thing twice"

`kv dupes` keeps them apart because each asks for a different decision.

1. **exact bytes**: one artifact at several paths. Not a relation and not a proposal: the library holds one item and the files are copies of it.
2. **identical text**: different bytes (another download, another PDF producer) whose extracted text is the same letter for letter. Proposed as `same_document` plus `equivalent_to` on the artifacts, confidence `exact`.
3. **same publication**: one accepted DOI under several documents, or the same title and first author. A shared DOI whose titles agree is `high`; with differing titles `medium`. Two different DOIs are two publications however alike.
4. **related work**: a preprint and its published version (`version_of`, `high`), or the same title under different DOIs (`related_to`, `low`).

A text fingerprint is the SHA-256 of a document's whole text reduced to letters and digits, so line breaks, hyphenation and
spacing do not matter. A text with fewer than 400 letters and digits has no fingerprint (a scan's stray page number proves
nothing by being equal).

## Supplements, corrections, chapters

* A **supplement** announces itself on its first page ("Supporting Information", "Supplementary Material") and names its paper by that paper's DOI or its printed title. A file name such as `_si` nominates a document to be examined and, alone, never decides.
* A **correction, comment or reply** is `related_to` the work it is about, not a `supplement_of` it, though it may mention the paper's supporting information. Such pairs are also kept out of the DOI-merge proposals.
* A **chapter** of a book held in the library is `part_of` it, when the record types say so (a member that merely carries the shared DOI is not the parent of its siblings). Chapters or entries of a book that is *not* in the library are proposed as a `collection` named by what the records say the book is.
* A DOI that three or more documents each print as their own is a parent work's, not any one document's; those documents are never proposed as copies of each other.

## Merge and split

Merging moves the absorbed document's artifacts to the survivor (as `alternate_copy`, never canonical), **retires** the absorbed
document (`retired_at`, `merged_into`; nothing is deleted), and carries over what hung off it: relations, collection membership,
tags, and accepted metadata values the survivor lacks (a survivor's own value is never overwritten, a lock is kept). Open
proposals that named the absorbed document are re-pointed at the survivor, because their evidence did not change. A proposal that
only said "these two are one" is marked accepted by `merge:<actor>`. Every artifact id is unchanged.

`kv document split` takes one artifact out into a document of its own. If that artifact used to be a document a merge retired,
that document is **revived under its original id**; otherwise a new one is made. A document merged away *before* its survivor was
itself merged still revives: a later merge re-points every earlier `merged_into` at the live survivor, so a retired document never
points at another retired one. `document_event` records every merge and split.

`kv explain` on a retired document id follows the merge to the survivor.

## Collections, tags, saved searches, views

* A **collection** is a named set of documents; a **tag** is a word on a document. Names compare by NFKC case-folded, whitespace-collapsed key. A tag keeps its punctuation (`C` and `C++` differ) and the first spelling used.
* A **saved search** stores the parsed query and the query-language version, not results. A search saved under a newer language than this program understands is refused, not guessed at.
* A **view** (`inbox`, `unresolved`, `ambiguous`, `missing`, `duplicates`, `new`, `untagged`, `uncollected`) is a computed query, never stored state: a document leaves it the moment the reason is gone. The inbox lists unresolved first, then ambiguous, missing, new.
* None of this touches a file.

## Search filters (query language 2)

`author: year: doi: kind: tag: collection:` narrow a search. They read **accepted** metadata, tags and collections only: an
unaccepted guess cannot narrow a search. Only these six names at the start of a word are filters, so `Cu(II):` or `pH:7.4`
stay search text; quote a phrase to search for `"year:2020"`. Filters are ANDed. A malformed filter is `KV_QUERY_INVALID`, never a
search that ignores it, and the result's `scope.documents_matching` says how many documents the filters admitted.

## What was found on the real library (2026-10-07, scratch catalog, nothing accepted)

1,841 documents, 17 exact-copy groups (the same file at up to 14 paths: an encyclopedia held as about a dozen volumes duplicated
across folders). `kv relate` proposed 150 identical-text merges, all `exact`; a rehearsal accepting every one on a copy merged 134
and found 16 already redundant (the earlier merges had made them true), left all 1,841 artifacts in place, and `kv doctor`
reported nothing afterwards. Four supplements and four corrections were proposed and hand-checked correct, and two collections
(shared-DOI parents). Two defects only this data showed were fixed: SICI-style DOIs containing `<`/`>` were corrupted by markup
stripping, and a merge made the open proposals about the absorbed document stale.
