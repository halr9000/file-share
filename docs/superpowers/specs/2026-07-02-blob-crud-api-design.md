# file-share Blob CRUD API — Design

## Context

`file-share-serve.py` (now `file_share_serve.py`) serves `/home/halr9000/shared/` to
Hal's tailnet at `https://neo.taileffc7.ts.net/files/`, with a preview UI and a
text-annotation feature (`/files-api/annotations`).

Files are currently added by Jeeves running raw shell (`cp`/`mv`) directly into the
shared directory. This has caused two problems:

1. **Ad hoc subdirectories.** The directory accumulated `vcc/`, `docs/`, `media/`,
   `temp/` categories with no enforcement, and three inconsistent path conventions
   existed simultaneously (absolute fs path in shell examples, share-relative path
   in annotation `file` keys, proxy-prefixed URL). This was already fixed in a prior
   pass: the directory is now flat and annotations were remapped to
   `/filename.ext` keys.
2. **No unique file identity.** A flat namespace means two files can't share a name.
   `cp` gives no feedback and no place to hang metadata (id, created_at) outside the
   filename itself.

This spec replaces "Jeeves runs `cp`" with a real CRUD API. Storing a file returns a
blob descriptor (id, filename, url, size, mime, created_at) to the caller, and
distinct blobs may share a filename because identity is the id, not the name.

## Goals

- Multiple files may have the same `filename`; each still gets a stable, distinct URL.
- Creating a file is an API call that returns machine-readable confirmation
  (not "hope `cp` worked").
- Structurally prevent subdirectories from reappearing — not just document against
  them, but make the write path unable to produce one.
- Revising a doc that has annotations (the existing drain-and-rewrite workflow)
  becomes one API call instead of a hand-rolled three-step dance.
- Existing `/files/*` browsing (preview pages, `?raw=1`, directory index) and the
  `/files-api/annotations` API keep working, adapted to the new id-keyed identity.

## Non-goals

- No rename-only / metadata-only PATCH on blobs (out of scope; revisions replace
  content via PUT, which is sufficient for the known workflows).
- No new auth layer — writes stay trusted-by-tailnet-membership, matching the
  existing annotation API's model (explicit decision, not an oversight).
- No upload size limit beyond what already implicitly existed via unbounded `cp`.
- No content-addressing (hash-based dedup) — ids are random, not derived from content.

## Storage & identity

Every blob lives directly under `/home/halr9000/shared/` as:

```
<id>-<filename>
```

- `id` = 8 lowercase hex chars (`uuid.uuid4().hex[:8]`), regenerated on the
  (practically negligible) chance of collision with an existing on-disk id.
- `filename` is the caller-supplied original name, validated on write:
  reject if it contains `/`, `..`, a NUL byte, or is empty/starts with `.`.
  This is the actual enforcement mechanism for "no subdirectories" — the only
  sanctioned write path (`POST`/`PUT /files-api/blobs`) cannot produce a nested
  path, so the failure mode from before is structurally closed, not just documented
  against.
- No separate manifest/index file. The blob list is derived by scanning
  `SHARED_DIR` for entries matching `^[0-9a-f]{8}-.+`; `.annotations.json` and any
  non-matching file (there shouldn't be any post-migration) are skipped.
- Annotation `file` keys switch from a share-relative path string (`/foo.md`) to
  the blob `id` (`"8b7e4f02"`). This is strictly more robust than path matching —
  ids don't drift when a file is renamed or reformatted, and there's no longer a
  three-way format to keep in sync (fs path / annotation key / URL all derive from
  the same id).

## API

All new endpoints live under `/files-api/blobs`, alongside the existing
`/files-api/annotations` (unchanged in shape, just now keyed by id).
`/files/*` (preview pages, raw download, directory index) is unchanged in
behavior — it just resolves `<id>-<filename>` paths instead of plain names.

| Method | Path | Request | Response |
|---|---|---|---|
| `POST` | `/files-api/blobs?filename=<name>` | raw bytes body | `201 {id, filename, url, size, mime, created_at}` |
| `GET` | `/files-api/blobs` | — | `200 [{id, filename, url, size, mime, created_at}, ...]` |
| `GET` | `/files-api/blobs/<id>` | — | `200 {...}` / `404` |
| `PUT` | `/files-api/blobs/<id>` | raw bytes body | `200 {id, filename, url, size, mime, created_at, drained_annotations}` / `404` |
| `DELETE` | `/files-api/blobs/<id>` | — | `204` / `404` |

**`filename` validation** (applies to `POST`'s query param): reject with `400` if
empty, contains `/`, contains `..`, contains a NUL byte, or starts with `.`.

**`PUT` semantics:** replaces the file's bytes in place. Same `id`, same `filename`,
same URL — Hal's existing link keeps working and now shows new content. As part of
the same call, the server deletes every annotation currently keyed to that `id`
(the "drain") and reports how many were cleared as `drained_annotations` in the
response. The server does *not* generate the human-readable "Annotation Log"
section — composing that into the new content before calling `PUT` is still
Jeeves' judgment call (summarizing upvotes/downvotes/comments, deciding
"Addressed?"), matching today's workflow. The mechanical bookkeeping (clearing
stale entries) moves to the server; the editorial judgment stays with the agent.

**`DELETE` semantics:** removes the file from disk and cascades deletion of any
annotations keyed to that `id`.

**Directory index UI** (`GET /files/` — human browsing): display filenames with the
id prefix stripped for readability (`report.md`, not `a1b2c3d4-report.md`), each
linking to the real id-prefixed URL.

## Migration

The 23 files already flattened into `shared/` (plain filenames, no id) and the 15
existing annotations (across 4 files) get migrated in one pass:

1. For each existing file, generate an id, `mv` it to `<id>-<filename>`.
2. Walk `.annotations.json`, remap each entry's `file` value from its old
   share-relative path (e.g. `/neo-proxy-design.md`) to the new blob id assigned
   to that file in step 1.
3. Verify: annotation count unchanged (15), every remapped key resolves via
   `GET /files-api/blobs/<id>`, restart `file-share.service`, spot-check a
   previously-annotated file's annotations still return via the API.

This is the second URL-breaking change in this session (after the flatten). Already
confirmed with Hal: nothing outside this repo hardcodes the current URLs, so this
is safe.

## Error handling

- Invalid `filename` on `POST` (slash, `..`, empty, leading dot) → `400`
- `PUT`/`GET`/`DELETE` on an unknown `id` → `404`
- Malformed/missing body on `POST`/`PUT` → `400`

## Skill doc changes

`workspace/skills/file-share/SKILL.md` is rewritten so the CRUD API is the *only*
documented way to add/update/remove a file — the `cp`/`mv`/`rm` examples are
removed entirely, not just de-emphasized. Also:

- Frontmatter description rewritten to lead with "Use when..." and add
  annotation/feedback/review keywords (currently missing despite annotations being
  roughly half the skill's content).
- Architecture section gains the `/files-api/blobs` route alongside the existing
  `/files-api/annotations` one and the already-undocumented `/files-api/annotations`
  route fix.
- Stale "Current shared files (as of setup)" table is deleted (already inaccurate
  before this change; actively wrong after it).
- New "Common Mistakes" section: don't hand-write files into `shared/` outside the
  API, id is the identity not the filename, drain happens automatically on `PUT`
  so don't also hand-roll the old three-step dance.

## Testing

Extend `tests/test_annotations.py` (or add `tests/test_blobs.py`) covering:

- `POST` assigns a valid id, returns correct metadata, rejects a filename with `/`
  or `..` with `400`.
- Two `POST`s with the same `filename` produce two distinct ids/URLs and both
  remain independently readable.
- `PUT` replaces content at the same URL, drains only that blob's annotations
  (a second blob's annotations are untouched), returns accurate `drained_annotations`.
- `DELETE` removes the file and cascades annotation deletion; second `DELETE`
  returns `404`.
- `GET /files-api/blobs` lists all blobs with correct metadata after a mix of
  create/update/delete operations.

Post-migration smoke test against the live service: fetch a migrated file at its
new id-prefixed URL, confirm a previously-annotated file's annotations resolve
under its new id key.
