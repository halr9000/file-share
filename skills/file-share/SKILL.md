---
name: file-share
description: >
  Use when: sharing a file with the user's devices, generating a share URL,
  adding, replacing, or removing a file from the shared object store, listing
  what's currently shared, reading or summarizing the user's
  annotations/feedback/comments on a shared doc, or revising a doc based on
  their annotations. Triggers on: "share this file", "give me a link to",
  "what files are shared", "remove from shared", "feedback on this doc",
  "address the comments on", "drain annotations", "revise based on
  annotations".
---

# file-share

Talks to a file-share server: a small HTTP file server that stores uploaded
files as a flat "blob store" (`<id>-<filename>`) and exposes a CRUD API for
managing them, plus a browser preview page where a human can leave inline
annotations (upvote/downvote/comment) for the agent to read back.

See the repo's `README.md` for what this server is and how to run/deploy it.
This file is the operational guide for an agent that already has one running
somewhere and needs to use it.

## Finding the server

You need one thing: the server's base URL (scheme + host, no trailing
slash) — e.g. `https://files.example.com` or `http://localhost:3458`.

**Check the `FILE_SHARE_BASE_URL` environment variable first.** If it's set,
use it. If it's not set and you don't already know the base URL from earlier
in this conversation or from project config, **ask the user** rather than
guessing a domain or port.

All examples below use `$FILE_SHARE_BASE_URL` as a placeholder — substitute
the real value.

## Storage model — flat blob store, not a filesystem

The data directory has **no subdirectories** and never should. Every file is
stored as `<id>-<filename>` (an 8-hex-char id, a hyphen, then the original
filename), which is what lets two files share the same `filename` while
staying distinct. `id` is the file's real identity — use it (not the
filename) whenever you need to reference a specific file (e.g. as the
annotation lookup key).

**Never write directly into the data directory with `cp`/`mv`/`echo >`** (even
if you have filesystem access to the host). The only sanctioned way to add,
replace, or remove a file is the CRUD API below — it's what assigns the id,
validates the filename can't create a nested path, and keeps annotations in
sync. A stray `mkdir` or `cp` bypasses all of that.

## Operations

### Add a file and get its URL

```bash
curl -s -X POST --data-binary @/path/to/file.ext \
  "$FILE_SHARE_BASE_URL/files-api/blobs?filename=file.ext"
```

Returns:
```json
{"id": "a1b2c3d4", "filename": "file.ext", "url": "/files/a1b2c3d4-file.ext",
 "size": 1234, "mime": "text/plain", "created_at": "2026-07-02T14:00:00+00:00"}
```

The share URL is `$FILE_SHARE_BASE_URL` + `url` from the response — e.g.
`https://files.example.com/files/a1b2c3d4-file.ext`.

### List all shared files

```bash
curl -s "$FILE_SHARE_BASE_URL/files-api/blobs" | jq .
```

Or browse the directory index (shows clean filenames, ids hidden):
```
$FILE_SHARE_BASE_URL/files/
```

### Look up one file's metadata by id

```bash
curl -s "$FILE_SHARE_BASE_URL/files-api/blobs/<id>" | jq .
```

### Replace a file's content (same URL, auto-drains its annotations)

```bash
curl -s -X PUT --data-binary @/path/to/new-version.ext \
  "$FILE_SHARE_BASE_URL/files-api/blobs/<id>"
```

Same `id`, same URL — an existing link keeps working and now shows the new
content. Any annotations on the old content are deleted server-side as part
of this call (`drained_annotations` in the response tells you how many).
**You still have to write the "Annotation Log" section into the new content
yourself before calling PUT** — see the drain protocol below; the server only
handles clearing the stale entries, not summarizing them.

### Remove a shared file

```bash
curl -s -X DELETE "$FILE_SHARE_BASE_URL/files-api/blobs/<id>"
```

Removes the file and cascades deletion of any annotations on it.

## Preview vs. raw URLs

By default, file URLs serve a GitHub-Gist-style HTML preview page with
syntax highlighting, rendered markdown, inline images/audio/video, and
Raw/Copy/Download buttons. To get the **raw file** (direct download), append
`?raw=1`:

```
# Preview page (default):
$FILE_SHARE_BASE_URL/files/a1b2c3d4-example.md

# Raw download:
$FILE_SHARE_BASE_URL/files/a1b2c3d4-example.md?raw=1
```

When sharing a file URL with the user, use the plain URL (preview page). If
they need a direct download link, append `?raw=1`.

## Security notes

- Auth model depends entirely on how the operator deployed the server — this
  skill makes no assumptions. It's typically run on a private network (home
  LAN, VPN, tailnet) with no additional auth on top; if you're unsure what's
  actually protecting this deployment, ask rather than assume.
- Do NOT put files containing secrets (API keys, tokens, passwords) into the
  store unless the user explicitly asks and understands who else can reach
  this deployment.
- `POST`/`PUT`/`DELETE` on `/files-api/blobs` typically have no auth beyond
  whatever network boundary protects the deployment — treat any endpoint as
  reachable by anyone who can reach the server at all.

## Annotations

Annotations are stored server-side, keyed by blob id. Each entry:

````json
{
  "id": "uuid",
  "file": "a1b2c3d4",
  "selected_text": "the annotated text",
  "offset_start": 0,
  "offset_end": 11,
  "type": "upvote | downvote | comment",
  "comment": "optional comment body",
  "author": "whoever left it",
  "created_at": "2026-06-10T19:00:00Z"
}
````

`file` is the blob **id** (not a path or filename — see Storage model above).

### Read all annotations for a file

```bash
curl -s "$FILE_SHARE_BASE_URL/files-api/annotations?file=<id>" | jq .
```

### Summarize annotations on a shared doc

When asked to review a shared document with annotations:
1. Fetch the file: `curl -s "$FILE_SHARE_BASE_URL/files/<id>?raw=1"`
2. Fetch its annotations: `GET /files-api/annotations?file=<id>`
3. Cross-reference `selected_text` + `offset_start/end` with the file content
4. Surface upvotes (liked sections), downvotes (sections to revise), comments (specific feedback)

### Rewrite a doc that has annotations (drain protocol)

**Required before calling PUT on any file that has annotations.** The
offsets in each annotation only make sense against the content they were
made on — once you replace the content, they're stale, and PUT clears them
automatically. If you don't preserve them somewhere first, the feedback is
gone.

1. **Fetch current annotations** for this blob id:

    ```bash
    curl -s "$FILE_SHARE_BASE_URL/files-api/annotations?file=<id>" | jq .
    ```

2. **Compose the new document content with an `## Annotation Log` appended**
   (before the final newline, or after the last content section):

    ```markdown
    ## Annotation Log
    *(Auto-generated from annotations present before this revision — cleared on PUT)*

    | # | Type | Selected text | Comment | Addressed? |
    |---|------|---------------|---------|------------|
    | 1 | upvote | "some phrase" | — | Retained |
    | 2 | downvote | "another phrase" | — | Removed |
    | 3 | comment | "a heading" | add a section here | See new section below |
    ```

    Include every annotation. For `type=comment`, carry the `comment` text
    verbatim. The "Addressed?" column is your judgment call.

3. **PUT the new content**:

    ```bash
    curl -s -X PUT --data-binary @new-version.md \
      "$FILE_SHARE_BASE_URL/files-api/blobs/<id>"
    ```

    The response's `drained_annotations` count should match what you saw in
    step 1 — if it doesn't, something raced (e.g. a new annotation was added
    between steps 1 and 3); re-check before reporting done.

### Revising vs. replacing

`PUT` always replaces content at the same id/URL. If you want to keep the old
version around instead of overwriting it (e.g. the user is mid-review and
wants to compare), create a **new** blob instead of PUTting — share the new
URL, leave the old one and its annotations untouched.

## Common mistakes

- **Writing into the data directory with `cp`/`mv` instead of the API.** This
  skips id assignment, can silently collide with another file's name, and
  won't be visible to `GET /files-api/blobs`. Always use the API.
- **Creating a subdirectory.** The store is flat by design — use a
  descriptive filename, not a category folder. The API's filename validation
  rejects `/` anyway; don't work around it by shelling out to `mkdir` + `mv`.
- **Confusing the blob id with the filename.** Annotation lookups, `PUT`, and
  `DELETE` all key on the id (`a1b2c3d4`), not the filename (`report.md`) —
  two files can have the same filename but never the same id.
- **Skipping the drain protocol.** Calling `PUT` without first writing an
  Annotation Log into the new content silently discards existing feedback —
  the annotations are gone from the store the moment `PUT` succeeds.
- **Forgetting `?raw=1`** when a direct download link is needed instead of
  the preview page.
- **Hardcoding a base URL.** Read `FILE_SHARE_BASE_URL` (or ask) instead of
  assuming a domain — this skill is meant to work against any deployment.
