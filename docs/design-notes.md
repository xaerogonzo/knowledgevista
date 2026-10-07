# Design notes backlog

Review points and ideas that are **not** part of the current architecture baseline. Each is triaged here instead of
being folded into the plan; an entry reopens the architecture only if it contradicts an invariant
([INVARIANTS.md](INVARIANTS.md)).

## Deferred, with the trigger that reopens each

| Idea | Reopen when |
|---|---|
| A `work` table (bibliographic work over editions/manifestations) | `version_of` / `part_of` cannot express a real case without being abused |
| Creator / ORCID normalisation | author disambiguation is actually requested |
| Embeddings, semantic search, AI summaries | deterministic search is trusted. Always derived, versioned, optional; semantic hits must still show source document and page |
| Watch mode (with debounce) and auto-organize policies | the organizer has survived real use. Policies would be declarative and versioned |
| Explorer context menu, file association, OS URI registration | the `knowledgevista://` scheme has real consumers |
| Cloud placeholder (OneDrive etc.) handling | a user reports an unmaterialised file indexed as empty |
| NAS / sync / encrypted vault | a second machine is actually in use |
| Graph view, citation graph | relations have enough accepted edges to be worth drawing. A citation graph is a different thing from file relations |
| Parallel extraction | a measurement shows a 739-file scan is too slow. The worker/single-writer split already allows it |
| `kv backup` / `kv restore` convenience | users ask; `kv export` and the migration backup cover the need until then |
| Read-status and reading-list entities | a "To Read" collection stops being enough |
| Hard-link browse folder | a concrete workflow needs it. A hard link is the same file: editing it edits the original, so it would be labelled that way |
| A migration script that rewrites OpenChem's recorded `file` names from the organizer journal | the organizer (milestone 6, built) has renamed a held file in a library OpenChem reads, and OpenChem PR #246 (`index_literature --check` through `kv locate`) is merged. Until then `kv locate` by hash covers a moved file and nothing needs rewriting |
| Organizer on a UNC (network) root | a library on a share exists to test against. Path composition for UNC and extended-length forms is tested; no real share was available, so a move on one is unproven |
| A Recycle-Bin duplicate-removal action | exact copies are a real burden. It would be a separate action with its own plan, never part of rename/move |
| Naming policies beyond `naming-1` (editions, DOI in the name, other layouts) | a person asks for a layout the two built ones do not give; a new policy is a new version, and old plans go stale by design |
| Page-targeted open in an external viewer | the built-in reader (milestone 8) exists; an arbitrary default viewer cannot be told which page |
| Per-root policies beyond `allow_organize` | multiple roots with different needs exist |
| Chemistry-aware search normalisation | opt-in only; default search never rewrites scientific terms |
| More metadata providers (OpenAlex, Open Library, arXiv, DataCite) | a library has many documents Crossref has no record of (the real one: books, theses, old reports). The `MetadataProvider` protocol and the state/cache/pacing rules already exist; a provider is a normaliser plus a URL |
| Chapter-level lookup for entries whose parent DOI is shared (title + container query) | the shared-DOI check finds many short-titled entries (the real library: 160 encyclopedia entries). A title under 20 letters is never searched for today, so they stay untitled by DOI until this exists |
| Persisting filename hints beyond a title (author + year such as `kaya2022`) as supporting evidence in matches | a match is wrong or missed in a way an author/year hint would have separated |
| Old Wiley "SICI" DOIs containing `<` and `>` | one is seen in a real library. The DOI pattern stops at those characters (as the OpenChem index did), so such a DOI is cut short and then fails the provider check, costing a missed match and never a wrong one |
| Bulk review shortcuts (`kv review accept --field title --confidence medium`) | a person actually faces hundreds of layout-only titles (the real library has ~380) and wants one decision for a class |
| Applying a plan from the window (`kv apply` as a button) | a person asks for it AND the plan preview has been used on a real library for a while. The window proposes and saves; the one mover stays a command with a dry run |
| Removing a document from a collection, deleting a collection, saved searches, and multi-select tag/collect in the window | a person manages collections in the window often enough that the command line is the friction (the services exist: `organize.remove_from_collection`, `retire_collection`, `save_search`) |
| Multi-select accept/reject in the Review tab | the queue is long and the safe filter plus "accept all safe" is not enough (the real library: 1,488 waiting, 13 safe) |
| An online-lookup button | it needs its own consent screen that lists exactly what is sent (the `--list-requests` output) before anything leaves the machine; until then `kv resolve --online` is the only door |
| A stopped scan shows once in Health as an "interrupted scan" | a person stops scans often. `interrupted_scans` counts every `interrupted` run (a crash and a deliberate Stop look the same in the catalog); a `stopped` status would separate them |
| A keyboard map and an accessibility pass (tab order, screen-reader names) | the window has a user who needs it; every control already has an object name and a plain-text label |
| Page-targeted open and search-hit-to-page navigation | milestone 8, the built-in reader |
| A search that failed is remembered in the window's state and re-run (and re-fails) at the next start | it annoys someone; the fix is to remember only searches that succeeded |

## Open questions to settle by measurement

- Windows Job Object memory limit for extraction workers (milestone 2 spike), and the documented fallback if it is
  unavailable.
- Whether plain PyMuPDF text blocks give an acceptable "reflowed view" without `pymupdf4llm`.
- ~~Crossref's current rate limits and headers~~ Settled in milestone 3 by one live request: the response carries
  `x-rate-limit-limit`, `x-rate-limit-interval` and `x-concurrency-limit`, and the pacing reads them at runtime
  (`network/policy.py`); no number is written in the code.
- Whether `kv` as a command name collides with anything on PATH once packages are published.
