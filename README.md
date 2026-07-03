# file-share

A tiny, dependency-free HTTP file server that stores uploaded files as a flat
"blob store" (`<8-hex-id>-<filename>`), serves them with a GitHub-Gist-style
HTML preview page, and exposes a small CRUD API for managing them. Includes
an inline annotation feature — visitors can highlight text on a preview page
and leave upvotes/downvotes/comments, which agents can read back and act on.

Built for AI agents to manage shared files on a human's behalf (upload a
file, get a share URL, read feedback, revise), but works fine as a plain
file server for humans too.

## Requirements

Python 3.9+, standard library only. No `pip install` needed.

## Quick start

```bash
python3 file_share_serve.py            # listens on 127.0.0.1:3458, serves ./shared
FILE_SHARE_DIR=/path/to/data PORT=8080 python3 file_share_serve.py
```

Browse to `http://127.0.0.1:3458/files/`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `FILE_SHARE_DIR` | `./shared` (created automatically if missing) | Directory the server reads/writes blobs from. No subdirectories — flat by design. |
| `PORT` | `3458` | Port to listen on. Can also be passed as `argv[1]`. |

The server only binds to `127.0.0.1` — put it behind whatever reverse proxy
or TLS terminator you like (nginx, Caddy, Tailscale Serve, etc.) to expose it
externally.

## Storage model

Every stored file becomes `<id>-<filename>` on disk, where `id` is an 8-hex-char
identifier. This lets two files share the same `filename` without colliding,
and gives each file a stable identity independent of its name. The directory
listing and preview pages show the clean `filename` — the `id` prefix is an
internal storage detail, not something users are meant to read or type.

**Never write directly into the data directory with `cp`/`mv`.** Always go
through the CRUD API below — it assigns the id, validates the filename can't
create a nested path, and keeps annotations in sync with file lifecycle.

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/files-api/blobs?filename=<name>` | Create a blob from the request body. Returns `201` + `{id, filename, url, size, mime, created_at}`. |
| `GET` | `/files-api/blobs` | List all blobs. |
| `GET` | `/files-api/blobs/<id>` | Get one blob's metadata. `404` if unknown. |
| `PUT` | `/files-api/blobs/<id>` | Replace a blob's content in place (same id/URL). Auto-deletes ("drains") any annotations on the old content; response includes `drained_annotations` count. |
| `DELETE` | `/files-api/blobs/<id>` | Delete a blob and cascade-delete its annotations. |
| `GET` | `/files-api/annotations?file=<id>` | List annotations for a blob id. |
| `POST` | `/files-api/annotations` | Create an annotation (used by the browser preview page). |
| `DELETE` | `/files-api/annotations/<id>` | Delete one annotation by its own id. |

No authentication beyond whatever network boundary you put in front of it —
this is designed for a private network (home LAN, VPN/tailnet), not the
public internet.

## Running tests

```bash
python3 -m unittest discover -s tests -v
```

## Migrating existing flat files into blob form

If you have a pre-existing flat directory of files (not yet in `<id>-<filename>`
form), `scripts/migrate_to_blobs.py` renames them in place and remaps any
existing `.annotations.json` file keys accordingly. Idempotent — safe to
re-run.

```bash
FILE_SHARE_DIR=/path/to/data python3 scripts/migrate_to_blobs.py
# or: python3 scripts/migrate_to_blobs.py /path/to/data
```

## Deployment

See `deploy/file-share.service.example` for a generic systemd user-service
template. Typical pattern: keep the git repo for development, and deploy a
copy of `file_share_serve.py` to a separate location (e.g. `/opt/file-share/app/`)
so the running service isn't tied to your working tree. Redeploy by copying
the file over and restarting the service.

## For AI agents

See `skills/file-share/SKILL.md` for a ready-to-use skill definition (works
with any agent harness that supports a `SKILL.md`-style skill file — Claude
Code, OpenClaw, or similar). It documents the CRUD operations, the
annotation "drain" protocol for revising annotated documents, and one
convention worth knowing up front: agents should read the `FILE_SHARE_BASE_URL`
environment variable (if set) to build full share URLs by prefixing it to the
`url` field the API returns — e.g. `$FILE_SHARE_BASE_URL` + `/files/a1b2c3d4-report.md`.
If it's not set, ask the user for the base URL rather than guessing.

## Backlog / ideas

- **Bounding-box image annotation.** Annotations currently anchor to a text
  character range (or nothing, for a general/unanchored comment). Extending
  the schema with an optional region (e.g. `{x, y, width, height}` as
  fractions of image dimensions) would let a viewer draw a box on an image
  preview and comment on that specific area, the same way text selection
  works for documents.
- **Optional real-time object detection to suggest bounding boxes.** As a
  follow-on to the above: run a lightweight local object-detection model
  (e.g. a YOLO variant) against uploaded images to propose candidate boxes
  a viewer could pick from instead of drawing one by hand. Strictly optional
  — the manual bounding-box flow above should work standalone without it.
- **Security sweep.** A focused pass over path handling, filename validation,
  and remaining `innerHTML` usages. Found and fixed one during this session:
  the annotation panel interpolated `selected_text`/`comment`/`author` into
  `innerHTML` unescaped (stored XSS via any annotation POST — no file-write
  access needed) — now built via DOM APIs (`textContent`) instead. Found but
  **not yet fixed**: the markdown table-of-contents flyout re-injects each
  rendered heading's `textContent` into `innerHTML`, which could produce
  broken/unexpected markup for a heading containing literal `<`/`&`
  characters (needs file-write access to exploit, since it comes from the
  document's own already-rendered heading text — narrower blast radius than
  the annotation bug, but still worth fixing the same way).
- **Cross-platform support (macOS, Windows).** The server itself is pure
  Python stdlib and should already run on both; what's unverified is the
  deployment story (the systemd example is Linux-only) and any path-handling
  assumptions (POSIX permissions, `/`-only path separators) that haven't been
  exercised outside Linux.
- **One-line quick-start installer** (`curl ... | bash` on macOS/Linux,
  `iwr ... | iex` on Windows) once this is published, so a first-time user
  doesn't need to manually clone + configure.
- **Beautify minified JS and HTML in the code preview**, matching the
  minified-JSON and minified-XML pretty-printing already implemented (both
  reformat safely using stdlib `json`/`xml.dom.minidom`). Reliable JS/HTML
  reformatting needs a real parser (regex-based reformatting is unsafe —
  easy to break on regex literals, template strings, unclosed void tags like
  `<br>`/`<img>`, etc.) which isn't available in the standard library without
  adding a dependency, conflicting with this project's zero-dependency goal.
  Worth revisiting if that constraint ever relaxes.

## License

MIT — see `LICENSE`.
