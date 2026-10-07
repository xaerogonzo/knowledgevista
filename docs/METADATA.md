# Metadata: proposals, evidence and the network

Milestone 3. This page is the policy; the code is `domain/doi_evidence.py`, `domain/local_evidence.py`,
`domain/match.py`, `services/resolve_metadata.py`, `services/review.py`, `services/metadata.py` and `network/`.

## The one rule

**A value is either a proposal or an accepted fact, and the two live in different tables.** `kv resolve` writes
proposals (`metadata_candidate`). An accepted value (`metadata_value`) exists only after a person accepts a proposal, a
person states it (`kv metadata set`), or one named rule (`safe_batch_v1`) accepts a proposal that earned `safe`. "Unknown"
is the absence of a row, never an empty string. Every accepted value keeps its origin, its source, who accepted it, a lock,
and a history row for every change with the previous value and where it came from.

Running `kv resolve` twice with nothing new changes nothing: a proposal is keyed by its *evidence*, an unchanged proposal is
not written, and neither is the catalog revision moved. A decision (accepted or rejected) is never reopened by finding the
evidence again.

## Sources

| Source | What it is | Can be `safe`? |
|---|---|---|
| `pdf_text_doi` | a DOI printed in the extracted text | yes (see below) |
| `pdf_metadata_doi` | a DOI the file's own Info/XMP names (untrusted; the only candidate for a scan) | no |
| `layout_title` | the largest title-sized text on page one that is not a masthead, banner, label or contents entry | only with a second, file-metadata source agreeing |
| `pdf_xmp_title`, `pdf_info_title` | the file's own title fields, with junk removed (application names, file names, identifiers) | only together with the layout title |
| `pdf_xmp_authors`, `pdf_info_authors` | name lists from the file's metadata (as often the uploader as the author) | never |
| `isbn_text` | an ISBN with a valid check digit on the first ten pages | never (a book has several) |
| `arxiv_text` | exactly one arXiv identifier on page one | yes |
| `filename_hint` | a file name that reads like a title (three or more words) | never |
| `crossref` | a Crossref record for a DOI, compared with the PDF | when the comparison is `exact` |
| `crossref_title_search` | a Crossref search result for the document's title, compared with the PDF | when `exact` and the record's authors also appear on the first pages |

## Which printed DOI is the document's own

A PDF prints many DOIs and only one names the paper. `domain/doi_evidence.py` scores every DOI printed outside the
bibliography; position is one feature, never the rule, and every component is stored so `kv explain` can show why.

| Component | Points | Meaning |
|---|---|---|
| `on_first_page` | +3 | printed on page one (+1 `on_second_page` if first on page two) |
| `publisher_cue_nearby` | +2 | "Cite this", "Received", "All rights reserved", the publisher's site, on the DOI's line or the line above or below |
| `first_on_page` | +1 | first DOI on page one |
| `repeated_on_pages` | +1 | printed on three or more pages (a running header) |
| `in_pdf_metadata` | +2 | the file's own metadata names it too |
| `names_another_work` | −3 | "DOI of original article", "Erratum to", "Comment on" on its own line |
| `crowded_first_page` | −2 | four or more DOIs on page one and no publisher words beside this one |
| `only_on_list_pages` | −3 | printed only on list pages (six or more DOIs: a cover's "articles you may like") |
| `wrapped_across_lines` | −1 | joined across a line break |

`own` needs 4 points and a margin of 2 over the next DOI; below that, 1 point or more is `ambiguous`, less is `foreign`.
A DOI after a "References" heading is counted but never proposed, unless publisher words sit beside it (a one-page erratum
has a heading above its own DOI). A DOI is `safe` locally only at 6 points or more, as the only own DOI, not wrapped, and not
contradicted or doubted by a provider.

**A DOI that three or more documents each call their own is a parent work, not any one of theirs.** Measured on the real
library: one encyclopedia's DOI was printed on the first page of 160 entries, each a separate PDF. It scored as "own" on
every one of them and the old index recorded it as each entry's DOI. `shared_dois` finds such DOIs over the whole library (so a
`--document` run agrees with a full one) and demotes them to `ambiguous`; only a provider record that matches *this* document
can then make one safe. A chapter or entry never inherits its parent's DOI.

## Comparing a provider's record with the PDF

A provider answers "what is the work with this DOI", never "is it the paper in my hand". `domain/match.py` compares the record
with the first two pages and reports a level with its components:

| Level | Means |
|---|---|
| `exact` | the title is printed (20 letters or more) and equals the layout title, an author of the record appears, and the year does not contradict |
| `strong` | the title is (nearly) all there and an author appears, but something stops short: the title is printed *inside a longer one* ("Correction to ..."), the year disagrees |
| `weak` | some of the title or authors |
| `contradicted` | neither the title nor any author appears |
| `unverifiable` | the record has nothing to compare |

Only `exact` is ever eligible for a batch rule. `strong` waits for a person, `weak` vetoes a locally safe DOI, and `contradicted`
sets the DOI aside (`status = set_aside`, remembered, so it is not proposed again). Similarity alone is the wrong test for
"same title": a short prefix on a long title scores above 0.9, so a record whose title sits inside a longer printed one by six
letters or more is a different title.

For a document with no usable DOI, a title search must produce a result whose title is nearly the title searched for *and*
which verifies against the pages. Between a published version and its preprint the published one is chosen and the preprint is
reported; two published results that verify equally are `ambiguous` and nothing is chosen. A title under 20 letters is never
searched for.

## The safe rule

`safe_batch_v1` (recorded as `decided_by = rule:safe_batch_v1`), for each field of a document, among its `safe` proposals:

- if they agree on one value, accept the best-sourced (agreeing proposals are accepted with it);
- if they disagree, accept nothing for that field;
- if the field has a value, accept only the same value or an *upgrade*: a value a rule accepted, replaced by the same letters
  from a better source (Crossref's "Cu2O" for the layout's "Cu 2 O"). A person's value, a locked value and any different
  value are never replaced by a rule.

## Privacy and the network

- **Online lookups are off.** `online_lookup` is a setting (`kv config set online_lookup true`), and `kv resolve --online` must
  also be asked for. A damaged settings file means off.
- **Only a DOI or a title is sent**, plus, if the user set one, their contact address (Crossref's "polite pool"). Never a
  file name, a path, a hash, an excerpt, an author list or page text. `kv resolve --online --list-requests` shows exactly what
  would be sent and sends nothing; `--online` prints the destination to stderr before it starts.
- `network/policy.py` owns the User-Agent, the timeout, retries with jitter, the `Retry-After` handling, the pace and the request
  budget. The pace is taken from the provider's own `x-rate-limit-*` headers, never from a number written in the code. A
  `Retry-After` longer than the run will wait stops the run for that provider instead of sleeping for minutes.
- **A provider that cannot answer is a state, not a result.** `rate_limited`, `offline`, `transient_error`,
  `provider_unavailable`, `unauthorized` and `not_attempted` are never recorded as "no match", never cached, and never change
  an accepted value or a stored proposal. Repeated failures stop the run (`complete: false`) and the next run resumes from the
  cache, which holds every definite answer (a work, or "no such DOI", for a shorter time) in the *cache* directory: deleting it
  loses no catalog state.
- Opening a document never triggers a lookup. Plain `kv resolve` never opens a socket (tested by making the transport fail
  the test if it is touched).

## How it was checked

Each line below was measured on a real 727-PDF library (private; nothing from it is in this repository), not asserted.

- **Against the old signal.** "The first DOI in the first two pages" found one for 482 PDFs (66%). The classifier calls 478 of
  them own and agrees with the old signal on 475; the three disagreements were the old signal's faults: a DOI cut at a line
  break (an incomplete `10.xxxx/journal`), and two with an invisible control character fused to the end of the DOI.
- **The 66% was inflated.** 160 of those 482 are entries of one encyclopedia, each a separate PDF printing the *encyclopedia's*
  DOI, which the old signal recorded as each entry's own. Excluding them, 318 PDFs print a DOI that is theirs; a further
  25 or so are undecided and the rest print none (books, old reports, scans). "Above 66%" could only have been reached by
  accepting 160 wrong DOIs, so the target is restated as *correct* accepted DOIs.
- **Local evidence alone** accepted 264 DOIs (36%) with no network, by the safe rule, and about 68 titles.
- **The full online run** (the user's decision, 2026-10-07; no contact address, nothing sent but DOIs and titles): 512 DOI lookups and
  313 title searches, 438 of them live requests (the rest answered from the cache of an earlier 80-document sample), 4 minutes,
  never stopped. Result: **393 of 727 PDFs (54%) have an accepted DOI** and 360 an accepted title; 355 have authors and 356 a year and
  a journal. By source: 264 from the file itself, 56 confirmed by Crossref (`exact`), 73 found by title. Of the 567 PDFs that are not
  entries of the encyclopedia (which have no DOI of their own), that is 69%. The remaining titles wait in the review queue
  (316 proposed and waiting, 47 with nothing that looked like a title, 4 scans). A settled library asks nothing: the third and fourth
  runs made 0 requests and changed nothing.
- **Hand-checked.** The 40 DOIs and 35 titles of the first sample, and then all 74 DOIs found by title search, against the printed first
  page: none wrong. Every one of the 264 locally accepted DOIs was re-run through the matcher against its Crossref record: 224 `exact`,
  36 `strong`, one unknown to Crossref (a typographic ligature, see below) and one `weak` (a Science paper whose PDF opens on its reference
  list); the last is reported by `resolve` as a problem and left for a person.
- **What the full run caught**, after the sample had passed: (1) ten documents were "confirmed" as the encyclopedia itself, because a
  book record has no authors and those files have no layout title, so nothing could have contradicted the match; an `exact` verdict now needs
  an author on the page, or a layout title that matches. (2) A DOI carried a typographic ligature from the PDF text (`j.ﬂuid`) and was
  accepted before any provider had been asked; DOIs are NFKC-normalised, and an accepted DOI a provider does not know, or only weakly matches,
  is now reported. (3) One title was a letter-spaced scan transcription (`T H E E F F E C T ...`); a run of four single-character words is
  never safe. (4) Documents found by title cost a second request each next run to fetch what the search had already delivered; a search result
  now also answers its own DOI lookup. Known limit: titles read from a scanned page's text layer inherit its OCR errors (a Roman numeral
  read as `11.`), and only a provider's `exact` match can replace them.
- **What the hand-check caught.** The first sample accepted two wrong titles (`doi:10.1016/...`: the XMP and Info titles of two
  papers both held the DOI, so two "independent" sources agreed on junk). That is why identifier-shaped titles are rejected and a
  safe title needs the layout among its sources. It also found 44 documents whose safe titles differed only by a non-breaking
  hyphen or a curly apostrophe, which the rule had read as a disagreement; same-words titles now agree.
- **Mutation testing.** 100 faults planted one at a time in the new code (a threshold, a cue window, the shared-DOI check, the
  offline switch, a cached failure, a dropped lock, an upgrade that ignores who accepted the value...). The first sweep let eight
  survive: six were missing tests, one was a clause that could never matter (removed), one a missing check; all are now caught. Thirteen more were planted after later changes (typographic agreement, the request counter, the fixes above) and all were caught.

## What comes next

The 160 entries sharing one parent DOI are the first real input to milestone 4: they are `part_of` proposals for that
book, to be accepted by a person, never merged. Until then each is a document with a title waiting in the review queue and
no DOI of its own.
