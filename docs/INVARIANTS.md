# Invariants

The rules everything else is checked against. A change that contradicts one of these needs the invariant argued
and rewritten first, not worked around.

1. **The filesystem is reality.** Filenames are locators, never identity.
2. **SHA-256 identifies concrete bytes** (an *artifact*). A **document** ID identifies a logical library item.
3. **Derivatives are rebuildable and declare their inputs.** Extractions, indexes, thumbnails, OCR, and any later
   embeddings or summaries each say which source artifact and which version of which algorithm produced them.
   Deleting every cache loses no user state.
4. **Uncertainty never silently becomes fact.** A proposal is not a fact. Accepted user state is distinct from
   inferred state. "The provider could not answer" is never "no match".
5. **Filesystem mutations are explicit, auditable and reversible.** Nothing is ever deleted.
6. **A missing file or unavailable root causes zero destructive catalog changes.** Only an explicit purge removes
   history.
7. **Knowledge Vista is useful with zero network.** Online lookup is opt-in and sends only a DOI or title.
   OpenChem Studio is useful with Knowledge Vista absent.
8. **MCP is read-only** until explicit, plan-based mutation exists.
9. **Reading and annotating never modify a source file.** Annotations live in the catalog.
10. **Any source-derived answer can navigate back to the original artifact and page.** A search snippet is a
    navigation aid, not a quotation; extracted table text is untrusted; the page is the source of truth.
11. **A user gets nearly all the value while leaving every file where it is.** Physical organisation is an optional
    presentation layer.

## Authority

| Thing | Authoritative for |
|---|---|
| The filesystem | current bytes and locations |
| The content hash | byte identity |
| Accepted catalog state | user-curated interpretation |
| Derivatives (extractions, indexes) | nothing: rebuildable |
| Proposals and candidates | nothing: suggestions |
| Caches | nothing: disposable |

No derived or historical record is authoritative merely because it exists in the catalog.

## Questions to ask of every feature

- Can this fact survive a rename? A move? A re-index? A replacement of the file by different bytes?
- Can it say where it came from? Can it be regenerated?
- What can go wrong, how would we detect it, how does it recover, and what evidence proves the recovery worked?
