# file-share

A tiny, dependency-free HTTP file server that stores uploaded files as a flat
"blob store" (`<8-hex-id>-<filename>`), serves them with a GitHub-Gist-style
HTML preview page, and exposes a small CRUD API for managing them. Includes
an inline annotation feature — visitors can highlight text on a preview page
and leave upvotes/downvotes/comments, which agents can read back and act on.

Built for AI agents to manage shared files on a human's behalf (upload a
file, get a share URL, read feedback, revise), but works fine as a plain
file server for humans too.

## Why this exists

It started as a quick way for an agent to share a markdown file while working
through a plan with me. I already have plenty of ways to read files off disk
— even from my phone — but what I actually wanted was: agent sends a Telegram
message, I tap the link, I'm reading the doc. No hunting through a filesystem
first.

Then, probably after one too many document reviews at work, I wanted to leave
comments the agent could read back — the same idea behind Antigravity's
review annotations. So the preview page grew an
annotation layer: highlight some text, leave an upvote/downvote/comment, and
the agent can fetch it and act on it.

From there it picked up the rest of what a preview page needs: line
wrapping and pretty-printing for minified JSON/XML, syntax highlighting,
markdown rendered to HTML, and inline image preview — with a "Raw" button
that always gets you the unmodified original file, straight download.
Image and video preview came essentially free once syntax-highlighted code
worked, since it's just native HTML5 `<img>`/`<video>`/`<audio>` — which
opens the door to some interesting image/video review and editing workflows
down the line.

It's tuned for mobile first (that's where the Telegram-link workflow lives)
but works just as well on a full-size screen for a longer review session.

The project ships with an agent skill (`skills/file-share/SKILL.md`) that
tells any agent how the CRUD and annotation APIs work, so once you're done
leaving feedback, the agent already knows where to find it — no separate
explanation needed each time.

If you're always working directly inside a coding agent with full
filesystem access (Codex, Claude Code, etc.), you probably don't need this —
you can just read the file. It earns its keep when you're working from a
messaging client instead, with no direct filesystem access of your own.

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

Phased so related items land in dependency order — each phase is small,
shippable, and doesn't block on a later one. Phases 1 and 2 are done; what's
left starts at Phase 3.

**Phase 3 — portability & distribution.** Verify cross-platform support
before building an installer on top of it, so the installer doesn't ship a
broken path for macOS/Windows.
- **Cross-platform support (macOS, Windows).** The server itself is pure
  Python stdlib and should already run on both; what's unverified is the
  deployment story (the systemd example is Linux-only) and any path-handling
  assumptions (POSIX permissions, `/`-only path separators) that haven't been
  exercised outside Linux.
- **One-line quick-start installer** (`curl ... | bash` on macOS/Linux,
  `iwr ... | iex` on Windows) once this is published, so a first-time user
  doesn't need to manually clone + configure.

**Phase 4 — bounding-box annotation.** The larger feature; object detection
is an explicit optional follow-on, not a prerequisite.
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

**Parked — not phased yet**
- **Beautify minified JS and HTML in the code preview**, matching the
  minified-JSON and minified-XML pretty-printing already implemented (both
  reformat safely using stdlib `json`/`xml.dom.minidom`). Reliable JS/HTML
  reformatting needs a real parser (regex-based reformatting is unsafe —
  easy to break on regex literals, template strings, unclosed void tags like
  `<br>`/`<img>`, etc.) which isn't available in the standard library without
  adding a dependency, conflicting with this project's zero-dependency goal.
  Blocked until that constraint relaxes.

## License

MIT — see `LICENSE`.
