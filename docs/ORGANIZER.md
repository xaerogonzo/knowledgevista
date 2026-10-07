# The organizer

Milestone 6. Knowledge Vista can rename and move the files in a library so that their names say what they are (`Examplar et al. (2021) -
Aqueous Solubility of Invented Esters.pdf` instead of `cm4c01978.pdf`), and take it all back. It is the only part of Knowledge Vista that moves a
user's file, so it is built around one idea: **everything that can go wrong is checked before anything moves, journaled while it moves, and
reconciled against the disk afterwards, and nothing is ever overwritten or deleted.**

You do not need it. Search, identification, relations, collections and the MCP server work on a library whose files never move
(invariant 11); this is an optional presentation layer over the filesystem.

```
kv root allow-organize papers        # a person says so, per root. The default is no.
kv plan create                       # PROPOSE: writes a plan file outside the library; moves nothing
kv plan show <plan> --status all     # read it
kv apply <plan> --dry-run            # every check, including hashing each source; moves nothing
kv apply <plan>                      # MOVE
kv undo                              # take the latest apply back, byte for byte
kv recover                           # after a crash: reconcile the journal with the disk
kv history [<operation>]             # what was done, when, by whom, with what outcome
```

## The lifecycle

`OBSERVE -> PROPOSE -> REVIEW -> PLAN -> PRECHECK -> APPLY -> VERIFY -> HISTORY`

Observe is the catalog (what a scan found). **Propose** is the naming policy. **Review** is you reading `kv plan show`. The **plan** is a file.
**Precheck, apply and verify** are `kv apply`. **History** is the journal. There is no auto-apply: nothing moves unless a person runs `kv apply`
on a plan they were given.

## What is planned

* Only roots with `allow_organize` (per root, by a person), enabled and online. Naming a root that does not allow it is an error that says how to allow it.
* Only documents with an **accepted** title. A proposal never names a file, and a document with no accepted title is left exactly as it is, listed in the
  plan as `unchanged: no accepted title`: an unknown stays unknown, and `Untitled (2021).pdf` would look like an answer.
* Every active location of every live document, so a file already named as the policy would name it shows as `unchanged`. The plan is the whole answer.
* A destination that is already occupied (on disk or in the catalog) is **`blocked`** with the reason, never overwritten and never silently renamed around.
  A destination held by a file the same plan moves away is fine: the executor orders chains.

## The naming policy (`naming-1`)

`<First author>[ et al.][ (<year>)] - <Title><.ext>`, from accepted metadata only. The rules, each pinned by a test (`tests/test_naming.py`):

* **Unicode** is NFC; format and control characters (a right-to-left override can make `exe.fdp` display as `pdf.exe`) are removed.
* **Characters Windows forbids** (`< > : " / \ | ? *`) are replaced (`:` `/` `\` `|` become ` - `, `"` becomes `'`) or dropped, so a name made here is valid wherever the library is copied.
* No trailing dot or space (Windows strips them silently, so two names become one file); **reserved device names** (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`, with or without an extension) get a `_`.
* **Length.** A stem over 120 characters is cut and ends `~` plus eight hex characters of the hash of the uncut stem, so two names that differ only past the cut still differ.
* A lead author typed as one string (`Examplar, A.`) is the part before the comma; a consortium with no comma is used whole.
* **Collisions** are resolved for the whole plan, deterministically, never by directory order: every member of a colliding group (compared NFC and case-insensitively, because
  the library may be copied to a filesystem that is) gets ` [<first eight characters of its artifact id>]` before the extension.

The same catalog and files give the same plan on any machine. `--layout by_year` additionally moves each file into `<year>/` (`unknown-year/` when the year is
unknown); the default renames in place. A plan records the policy version, and a plan made under another version is `stale`.

## The plan file

A frozen JSON document with its own `plan_id`, the hash of everything else, a format version (a newer one is refused, not half understood), the catalog revision,
the naming policy, the options, the roots it touches (with the identity they had: path key and volume), and one item per location. An item names the file by its
**SHA-256**, its current and wanted path, the operation (`rename`, `move`, `rename+move`, `unchanged`), the accepted metadata the name was built from, the source and
confidence of that title (`high` when a person stated it, `medium` when a rule or a resolver accepted it) and a risk (a move of a rule-accepted title is `high`).

It is written **outside** the library (refused inside any root: writing there would be a mutation of it) and **registered** in the catalog with its hash, so
`apply` can tell a plan this catalog made from a stranger's, and an edited plan from the original. It is never overwritten: a new plan is a new file.

**Stale** is decided per item, not by a revision number, so tagging a document does not invalidate a plan about file names. A plan is stale when a file is no
longer at the path it names (or is no longer that file), when the accepted title, authors or year it was built from changed, when the document was merged, or when
the naming policy or a root's identity changed. A stale plan is refused whole (`KV_PLAN_STALE`, with the items and the reasons) and nothing moves.

## Apply

Before the first move: the plan is validated, every root is the root the plan was made for and still allows organizing and is online, and no earlier operation is
unfinished (`KV_OPERATION_UNFINISHED`: run `kv recover` first). Then, per item:

1. **Precheck.** The destination is a legal name, inside the root once resolved (no `..`, no link that leaves), under 1,024 characters; **nothing on the source path is a
   symlink, junction or other reparse point** (a move through one moves something else); the source is a file and its **SHA-256 is what the plan says**, else the item is
   abandoned (`KV_FILE_CHANGED`: it is not the file that was reviewed); the destination is absent in the filesystem and the catalog, or is the same file under another
   case/Unicode form. A locked file (a viewer has it open) is **skipped and reported**, never a batch failure.
2. **The intent is committed** (`executing`, with any temporary name) before the filesystem is touched.
3. **The move** goes through `move_no_overwrite`, which fails rather than replace on every platform. A rename that looks like a no-op to the filesystem (`Foo.pdf` to `foo.pdf`)
   goes through a journaled temporary name in the same folder. A brief lock (an antivirus scan) is retried a few times with short delays, then reported locked.
4. **The outcome and the catalog's location update commit in one transaction**: the old location is ended `moved` with its successor, the new one is recorded,
   both with `organizer` history, so a later `kv scan` agrees (it sees an unchanged file) and `kv locate` follows the file.
5. **Verify**: the file is at the destination, the old name is gone, the size matches (`--verify-hashes` reads it again).

**Chains** (A to B while B goes to C) are ordered so the vacating move goes first. **Cycles** (a swap, a ring) have no first move, so one member is staged through a journaled
temporary name, the rest run, and it lands last. A rename never changes a document's identity, collections, tags, notes or relations: only its location.

Applying the same plan twice reports `already_applied` and changes nothing; after an undo the same plan applies again.

## Undo

`kv undo` is the same machinery with the paths swapped and one more rule: **the file at the new path must still be exactly the bytes that were moved.** If it was
edited or replaced since, undo refuses that item (`KV_FILE_CHANGED`) and says so; it never overwrites and never moves user work. It also refuses an item whose old path is
occupied (`KV_DESTINATION_EXISTS`). Folders the apply created (and recorded) are removed if they are empty; a folder that was there before is never touched. Undo
undoes an apply, never an undo (apply the plan again instead), and withdrawing a root's permission stops undo too.

## Interruption and recovery

The journal (`operation`, `operation_item`) is written before the filesystem is touched, so a crash can only fall in three places, each recoverable without guessing:

| The process died | The journal says | `kv recover` sees | And does |
|---|---|---|---|
| before the move | `executing` | the file at its old path | marks it not done (`KV_INTERRUPTED`) |
| after the move, before the bookkeeping | `executing` | the file at the new path, hash as expected | finishes the catalog and journal |
| mid-way through a staged move | `executing` + a temporary name | the file at that name, its old path free | puts it back where it started |

Anything else (the file is nowhere, or in two places) is **`uncertain`**: reported with what was seen, never retried, never decided again. `recover` never moves a file forward.
`kv doctor` (category `operations`) reports an unfinished operation, an uncertain item, and a successful move whose catalog location does not match.

## What it will not do

Delete anything (duplicate removal is a separate, Recycle-Bin action that does not exist yet); touch a root without `allow_organize`; follow a link; overwrite; write the plan
into the library; move a file it has not just hashed; or retry an ambiguous move. It does not watch folders and has no auto-apply policy.

## What was measured

See "Rehearsal on a copy of the real library" in the CHANGELOG and the Windows matrix in `tests/test_organizer_apply.py`: long (extended-length) paths, reserved and hostile names,
trailing dots and spaces, Unicode forms, case-only renames, targets differing only by case, read-only and hidden files, symlinks and junctions, locked files, an antivirus race,
chains, swaps and rings, a stale or edited plan, an interrupted apply, an `uncertain` item, a changed source, a destination that appeared, a repeated apply, and an undo after
the moved file was edited. UNC roots are tested as path composition only (no share was available); that gap is listed in docs/design-notes.md.

## Order of use (a safety rule from the plan)

The organizer is first run on a **copy** of a library, never the real folder, and not before OpenChem Studio can follow a renamed file (its `index_literature --check`
asks `kv locate`; see [INTEGRATION.md](INTEGRATION.md)). Nothing in this repository runs it on a real library.
