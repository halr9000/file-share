# Blob CRUD API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace raw `cp`/`mv` writes into `/home/halr9000/shared/` with a `/files-api/blobs` CRUD API on `file_share_serve.py`, so multiple files can share a filename (identity is an id, not the name), subdirectories are structurally impossible, and revising an annotated doc is one `PUT` instead of a hand-rolled three-step dance.

**Architecture:** Blobs are stored flat as `<8-hex-id>-<filename>` under `SHARED_DIR`, no manifest — the blob list is derived by scanning the directory. Annotation `file` keys switch from path strings to blob ids. New `do_PUT` method and new branches in `do_POST`/`do_GET`/`do_DELETE` handle `/files-api/blobs*`; `/files/*` (preview/raw/directory serving) is unchanged in logic, only in what display layer strips (the id prefix).

**Tech Stack:** Python 3 stdlib only (`re`, `uuid`, `json`, `pathlib`, `http.server`) — matches the existing file. Tests via `unittest` + `http.client`, same harness as `tests/test_annotations.py`.

## Global Constraints

- Blob id = `uuid.uuid4().hex[:8]` (lowercase hex, 8 chars), collision-checked against existing on-disk ids before use.
- Upload `filename` is rejected (`400`) if empty, contains `/`, contains `..`, contains a NUL byte, or starts with `.`.
- No auth beyond Tailscale network membership on any new endpoint (matches existing `/files-api/annotations` precedent — confirmed with Hal).
- No PATCH/rename endpoint — revisions go through `PUT` (replace content, same id) per spec's non-goals.
- Every new/changed behavior needs a test in `tests/`; run the full suite (`python3 -m unittest discover -s tests -v`) before the final commit of each task.

---

### Task 1: Blob identity & metadata helpers

**Files:**
- Modify: `file_share_serve.py:43` (imports), and insert new module-level code after `_store = AnnotationStore()` (currently line 196, before the `# ── 4. Utility functions ──` comment)
- Test: `tests/test_blobs.py` (create)

**Interfaces:**
- Produces: `parse_blob_name(name: str) -> tuple[str, str] | None`, `is_valid_upload_filename(filename: str) -> bool`, `generate_blob_id() -> str`, `blob_metadata(fs_path: Path) -> dict | None`, `list_blobs() -> list[dict]`, `find_blob_path(blob_id: str) -> Path | None`. All module-level in `file_share_serve.py`, used by every later task.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_blobs.py`:

```python
"""Tests for file-share blob CRUD helpers and API."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import file_share_serve as fss


class TestBlobHelpers(unittest.TestCase):

    def test_parse_blob_name_splits_id_and_filename(self):
        result = fss.parse_blob_name('a1b2c3d4-report.md')
        self.assertEqual(result, ('a1b2c3d4', 'report.md'))

    def test_parse_blob_name_handles_hyphens_in_filename(self):
        result = fss.parse_blob_name('a1b2c3d4-my-report-final.md')
        self.assertEqual(result, ('a1b2c3d4', 'my-report-final.md'))

    def test_parse_blob_name_rejects_non_blob_names(self):
        self.assertIsNone(fss.parse_blob_name('plain-name.md'))
        self.assertIsNone(fss.parse_blob_name('.annotations.json'))
        self.assertIsNone(fss.parse_blob_name('ZZZZZZZZ-report.md'))  # not hex

    def test_is_valid_upload_filename_accepts_normal_names(self):
        self.assertTrue(fss.is_valid_upload_filename('report.md'))
        self.assertTrue(fss.is_valid_upload_filename('my-file_v2.json'))

    def test_is_valid_upload_filename_rejects_slash(self):
        self.assertFalse(fss.is_valid_upload_filename('docs/report.md'))

    def test_is_valid_upload_filename_rejects_dotdot(self):
        self.assertFalse(fss.is_valid_upload_filename('../etc/passwd'))

    def test_is_valid_upload_filename_rejects_leading_dot(self):
        self.assertFalse(fss.is_valid_upload_filename('.annotations.json'))
        self.assertFalse(fss.is_valid_upload_filename('.hidden'))

    def test_is_valid_upload_filename_rejects_empty(self):
        self.assertFalse(fss.is_valid_upload_filename(''))

    def test_is_valid_upload_filename_rejects_nul_byte(self):
        self.assertFalse(fss.is_valid_upload_filename('bad\x00name.md'))

    def test_generate_blob_id_is_8_hex_chars(self):
        blob_id = fss.generate_blob_id()
        self.assertRegex(blob_id, r'^[0-9a-f]{8}$')


class TestBlobMetadata(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name)
        self._orig_shared = fss.SHARED_DIR
        fss.SHARED_DIR = self.shared

    def tearDown(self):
        fss.SHARED_DIR = self._orig_shared
        self.tmpdir.cleanup()

    def test_blob_metadata_for_valid_blob(self):
        p = self.shared / 'a1b2c3d4-report.md'
        p.write_text('hello')
        meta = fss.blob_metadata(p)
        self.assertEqual(meta['id'], 'a1b2c3d4')
        self.assertEqual(meta['filename'], 'report.md')
        self.assertEqual(meta['url'], '/files/a1b2c3d4-report.md')
        self.assertEqual(meta['size'], 5)
        self.assertIn('created_at', meta)

    def test_blob_metadata_returns_none_for_non_blob_file(self):
        p = self.shared / 'plain.md'
        p.write_text('hello')
        self.assertIsNone(fss.blob_metadata(p))

    def test_list_blobs_returns_only_blob_files_sorted_by_filename(self):
        (self.shared / 'b2222222-zebra.md').write_text('z')
        (self.shared / 'a1111111-apple.md').write_text('a')
        (self.shared / 'not-a-blob.md').write_text('x')
        (self.shared / '.annotations.json').write_text('[]')
        blobs = fss.list_blobs()
        self.assertEqual([b['filename'] for b in blobs], ['apple.md', 'zebra.md'])

    def test_find_blob_path_matches_by_id(self):
        p = self.shared / 'c3333333-notes.md'
        p.write_text('x')
        found = fss.find_blob_path('c3333333')
        self.assertEqual(found, p)

    def test_find_blob_path_returns_none_when_missing(self):
        self.assertIsNone(fss.find_blob_path('deadbeef'))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: `AttributeError: module 'file_share_serve' has no attribute 'parse_blob_name'` (and similar for the other new names)

- [ ] **Step 3: Add `import re` to the imports block**

In `file_share_serve.py`, the imports currently read (around line 36-46):

```python
import json
import os
import sys
import html
import mimetypes
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
```

Change to:

```python
import json
import os
import re
import sys
import html
import mimetypes
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
```

- [ ] **Step 4: Implement the helpers**

In `file_share_serve.py`, find this line (currently ~196):

```python
_store = AnnotationStore()
```

Insert immediately after it:

```python
_store = AnnotationStore()


# ── 3b. Blob identity & metadata ───────────────────────────
BLOB_NAME_RE = re.compile(r'^([0-9a-f]{8})-(.+)$')


def parse_blob_name(name: str) -> tuple[str, str] | None:
    """Split a stored filename like 'a1b2c3d4-report.md' into (id, filename).

    Returns None if name doesn't match the blob naming convention (e.g.
    '.annotations.json', or a pre-migration plain filename).
    """
    m = BLOB_NAME_RE.match(name)
    if not m:
        return None
    return m.group(1), m.group(2)


def is_valid_upload_filename(filename: str) -> bool:
    """Reject filenames that could escape the flat shared/ namespace.

    This is the actual enforcement for "no subdirectories in shared/" —
    the CRUD API is the only sanctioned write path, so rejecting '/' here
    makes a nested path structurally impossible to create through it.
    """
    if not filename:
        return False
    if '/' in filename or '\x00' in filename:
        return False
    if '..' in filename:
        return False
    if filename.startswith('.'):
        return False
    return True


def generate_blob_id() -> str:
    return uuid.uuid4().hex[:8]


def blob_metadata(fs_path: Path) -> dict | None:
    """Build the JSON-serializable metadata dict for a blob file.

    Returns None if fs_path's name doesn't match the blob naming convention.
    """
    parsed = parse_blob_name(fs_path.name)
    if parsed is None:
        return None
    blob_id, filename = parsed
    stat = fs_path.stat()
    mime, _ = mimetypes.guess_type(filename)
    if not mime:
        mime = 'application/octet-stream'
    return {
        'id': blob_id,
        'filename': filename,
        'url': f'{PREFIX}/{fs_path.name}',
        'size': stat.st_size,
        'mime': mime,
        'created_at': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def list_blobs() -> list[dict]:
    """Return metadata for every blob under SHARED_DIR, sorted by filename."""
    blobs = []
    for p in SHARED_DIR.iterdir():
        if not p.is_file():
            continue
        meta = blob_metadata(p)
        if meta is not None:
            blobs.append(meta)
    blobs.sort(key=lambda b: b['filename'].lower())
    return blobs


def find_blob_path(blob_id: str) -> Path | None:
    """Find the on-disk path for a blob id, or None if no blob has that id."""
    for p in SHARED_DIR.iterdir():
        if not p.is_file():
            continue
        parsed = parse_blob_name(p.name)
        if parsed and parsed[0] == blob_id:
            return p
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 6: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: add blob identity and metadata helpers"
```

---

### Task 2: `POST /files-api/blobs` (create)

**Files:**
- Modify: `file_share_serve.py` — `do_POST` method (currently ~line 1616)
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `is_valid_upload_filename`, `generate_blob_id`, `blob_metadata`, `_send_json` (Task 1 + existing)
- Produces: `POST /files-api/blobs?filename=<name>` — `201 {id, filename, url, size, mime, created_at}` on success, `400` on invalid filename.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_blobs.py` (new class, after `TestBlobMetadata`):

```python
class TestBlobAPI(unittest.TestCase):
    """Integration tests: spins up a real server, hits it with http.client."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        fss.SHARED_DIR = Path(cls.tmpdir.name)
        fss.ANNOTATIONS_FILE = fss.SHARED_DIR / '.annotations.json'
        fss._store = fss.AnnotationStore(fss.ANNOTATIONS_FILE)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), fss.GistHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmpdir.cleanup()

    def _post_blob(self, filename: str, body: bytes):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('POST', f'/files-api/blobs?filename={filename}', body=body,
                     headers={'Content-Length': str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if data else {}

    def test_post_creates_blob_with_metadata(self):
        status, body = self._post_blob('report.md', b'# Report')
        self.assertEqual(status, 201)
        self.assertRegex(body['id'], r'^[0-9a-f]{8}$')
        self.assertEqual(body['filename'], 'report.md')
        self.assertEqual(body['url'], f'/files/{body["id"]}-report.md')
        self.assertEqual(body['size'], 8)

    def test_post_two_blobs_same_filename_get_distinct_ids(self):
        _, first = self._post_blob('dup.md', b'one')
        _, second = self._post_blob('dup.md', b'two')
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['url'], second['url'])

    def test_post_rejects_filename_with_slash(self):
        status, body = self._post_blob('sub%2Fpath.md', b'x')
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_post_rejects_missing_filename(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('POST', '/files-api/blobs', body=b'x', headers={'Content-Length': '1'})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: `TestBlobAPI` tests fail with `404` (no `/files-api/blobs` route yet) instead of expected status codes.

- [ ] **Step 3: Implement `do_POST` blob branch**

In `file_share_serve.py`, find `do_POST` (currently ~line 1616):

```python
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/files-api/annotations':
            self._send_json(404, {'error': 'Not Found'})
            return
```

Change to:

```python
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/files-api/blobs':
            self._handle_create_blob(parsed)
            return

        if parsed.path != '/files-api/annotations':
            self._send_json(404, {'error': 'Not Found'})
            return
```

Then add a new method `_handle_create_blob` right before `do_POST`:

```python
    def _handle_create_blob(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        filename = query.get('filename', [None])[0]
        if not filename or not is_valid_upload_filename(filename):
            self._send_json(400, {'error': (
                'filename must be non-empty and must not contain "/", "..", '
                'a NUL byte, or start with "."'
            )})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        fs_path = None
        for _ in range(5):
            blob_id = generate_blob_id()
            candidate = SHARED_DIR / f'{blob_id}-{filename}'
            if not candidate.exists():
                fs_path = candidate
                break
        if fs_path is None:
            self._send_json(500, {'error': 'Could not allocate a unique blob id'})
            return

        fs_path.write_bytes(body)
        self._send_json(201, blob_metadata(fs_path))

    def do_POST(self):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: add POST /files-api/blobs to create blobs"
```

---

### Task 3: `GET /files-api/blobs` and `GET /files-api/blobs/<id>`

**Files:**
- Modify: `file_share_serve.py` — `do_GET` method (currently ~line 1688)
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `list_blobs`, `find_blob_path`, `blob_metadata` (Task 1)
- Produces: `GET /files-api/blobs` → `200 [...]`; `GET /files-api/blobs/<id>` → `200 {...}` / `404`

- [ ] **Step 1: Write the failing tests**

Add to `TestBlobAPI` in `tests/test_blobs.py`:

```python
    def _get(self, path: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET', path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def test_get_list_returns_all_blobs(self):
        self._post_blob('list-a.md', b'a')
        self._post_blob('list-b.md', b'b')
        status, body = self._get('/files-api/blobs')
        self.assertEqual(status, 200)
        filenames = {b['filename'] for b in body}
        self.assertIn('list-a.md', filenames)
        self.assertIn('list-b.md', filenames)

    def test_get_single_blob_by_id(self):
        _, created = self._post_blob('single.md', b'hello')
        status, body = self._get(f'/files-api/blobs/{created["id"]}')
        self.assertEqual(status, 200)
        self.assertEqual(body['filename'], 'single.md')

    def test_get_single_blob_unknown_id_returns_404(self):
        status, body = self._get('/files-api/blobs/deadbeef')
        self.assertEqual(status, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: `test_get_list_returns_all_blobs` and related tests fail (falls through to the `/files` static-serving branch and 404s incorrectly, or errors on JSON parse of an HTML 404 page).

- [ ] **Step 3: Implement `do_GET` blob branches**

In `file_share_serve.py`, find the annotation branch inside `do_GET` (currently ~line 1694):

```python
        # Annotation API
        if url_path == '/files-api/annotations':
            file_param = query.get('file', [None])[0]
            if file_param is None:
                self._send_json(400, {'error': 'Missing ?file= parameter'})
                return
            self._send_json(200, _store.get(file_param))
            return

        # Strip /files prefix
```

Change to:

```python
        # Annotation API
        if url_path == '/files-api/annotations':
            file_param = query.get('file', [None])[0]
            if file_param is None:
                self._send_json(400, {'error': 'Missing ?file= parameter'})
                return
            self._send_json(200, _store.get(file_param))
            return

        # Blob API
        if url_path == '/files-api/blobs':
            self._send_json(200, list_blobs())
            return

        if url_path.startswith('/files-api/blobs/'):
            blob_id = url_path[len('/files-api/blobs/'):]
            fs_path = find_blob_path(blob_id)
            if fs_path is None:
                self._send_json(404, {'error': 'Blob not found'})
                return
            self._send_json(200, blob_metadata(fs_path))
            return

        # Strip /files prefix
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: add GET /files-api/blobs list and single-blob lookup"
```

---

### Task 4: `AnnotationStore.delete_by_file()`

**Files:**
- Modify: `file_share_serve.py` — `AnnotationStore` class (currently lines 103-192)
- Test: `tests/test_annotations.py`

**Interfaces:**
- Produces: `AnnotationStore.delete_by_file(self, file_key: str) -> int` — deletes every annotation with `file == file_key`, returns count deleted. Used by Task 5 (`PUT`) and Task 6 (`DELETE`).

- [ ] **Step 1: Write the failing test**

Add to `TestAnnotationStore` in `tests/test_annotations.py`:

```python
    def test_delete_by_file_removes_all_matching_annotations(self):
        self.store.add('a1b2c3d4', 'foo', 0, 3, 'upvote', '', 'hal')
        self.store.add('a1b2c3d4', 'bar', 4, 7, 'comment', 'note', 'hal')
        self.store.add('other-id', 'baz', 0, 3, 'upvote', '', 'hal')
        count = self.store.delete_by_file('a1b2c3d4')
        self.assertEqual(count, 2)
        self.assertEqual(len(self.store.get('a1b2c3d4')), 0)
        self.assertEqual(len(self.store.get('other-id')), 1)

    def test_delete_by_file_returns_zero_when_no_match(self):
        count = self.store.delete_by_file('no-such-id')
        self.assertEqual(count, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_annotations -v`
Expected: `AttributeError: 'AnnotationStore' object has no attribute 'delete_by_file'`

- [ ] **Step 3: Implement `delete_by_file`**

In `file_share_serve.py`, find the `delete` method on `AnnotationStore` (currently ~line 171-179):

```python
    def delete(self, ann_id: str) -> bool:
        """Delete annotation by id. Returns True if found and deleted."""
        with self._lock:
            original_len = len(self._data)
            self._data = [a for a in self._data if a['id'] != ann_id]
            if len(self._data) < original_len:
                self._save()
                return True
            return False
```

Insert immediately after it:

```python
    def delete_by_file(self, file_key: str) -> int:
        """Delete every annotation for the given file key. Returns count deleted."""
        with self._lock:
            original_len = len(self._data)
            self._data = [a for a in self._data if a['file'] != file_key]
            deleted = original_len - len(self._data)
            if deleted:
                self._save()
            return deleted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_annotations -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_annotations.py
git commit -m "feat: add AnnotationStore.delete_by_file for blob PUT/DELETE drain"
```

---

### Task 5: `PUT /files-api/blobs/<id>` (update + auto-drain)

**Files:**
- Modify: `file_share_serve.py` — add new `do_PUT` method
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `find_blob_path`, `blob_metadata` (Task 1), `_store.delete_by_file` (Task 4)
- Produces: `PUT /files-api/blobs/<id>` → `200 {id, filename, url, size, mime, created_at, drained_annotations}` / `404`

- [ ] **Step 1: Write the failing tests**

Add to `TestBlobAPI` in `tests/test_blobs.py`:

```python
    def _put_blob(self, blob_id: str, body: bytes):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('PUT', f'/files-api/blobs/{blob_id}', body=body,
                     headers={'Content-Length': str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if data else {}

    def _post_annotation(self, file_key: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        payload = json.dumps({
            'file': file_key, 'selected_text': 'x', 'offset_start': 0,
            'offset_end': 1, 'type': 'upvote', 'comment': '', 'author': 'hal',
        }).encode()
        conn.request('POST', '/files-api/annotations', body=payload,
                     headers={'Content-Type': 'application/json',
                              'Content-Length': str(len(payload))})
        resp = conn.getresponse()
        resp.read()

    def test_put_replaces_content_same_id_and_url(self):
        _, created = self._post_blob('rev.md', b'v1')
        status, body = self._put_blob(created['id'], b'v2 longer')
        self.assertEqual(status, 200)
        self.assertEqual(body['id'], created['id'])
        self.assertEqual(body['url'], created['url'])
        self.assertEqual(body['size'], len(b'v2 longer'))

    def test_put_drains_only_that_blobs_annotations(self):
        _, blob_a = self._post_blob('drain-a.md', b'a')
        _, blob_b = self._post_blob('drain-b.md', b'b')
        self._post_annotation(blob_a['id'])
        self._post_annotation(blob_a['id'])
        self._post_annotation(blob_b['id'])

        status, body = self._put_blob(blob_a['id'], b'a-revised')
        self.assertEqual(status, 200)
        self.assertEqual(body['drained_annotations'], 2)

        _, remaining_a = self._get(f'/files-api/annotations?file={blob_a["id"]}')
        _, remaining_b = self._get(f'/files-api/annotations?file={blob_b["id"]}')
        self.assertEqual(remaining_a, [])
        self.assertEqual(len(remaining_b), 1)

    def test_put_unknown_id_returns_404(self):
        status, body = self._put_blob('deadbeef', b'x')
        self.assertEqual(status, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: `PUT` tests fail — `BaseHTTPRequestHandler` has no `do_PUT`, so the server responds `501 Unsupported method`, which isn't valid JSON and raises on `json.loads`.

- [ ] **Step 3: Implement `do_PUT`**

In `file_share_serve.py`, add a new method after `do_DELETE` (currently ends ~line 1661, right before `do_PATCH`):

```python
    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith('/files-api/blobs/'):
            self._send_json(404, {'error': 'Not Found'})
            return

        blob_id = parsed.path[len('/files-api/blobs/'):]
        if not blob_id or '/' in blob_id:
            self._send_json(404, {'error': 'Not Found'})
            return

        fs_path = find_blob_path(blob_id)
        if fs_path is None:
            self._send_json(404, {'error': 'Blob not found'})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        fs_path.write_bytes(body)

        drained = _store.delete_by_file(blob_id)
        meta = blob_metadata(fs_path)
        meta['drained_annotations'] = drained
        self._send_json(200, meta)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: add PUT /files-api/blobs/<id> to replace content and auto-drain annotations"
```

---

### Task 6: `DELETE /files-api/blobs/<id>` (remove + cascade)

**Files:**
- Modify: `file_share_serve.py` — `do_DELETE` method (currently ~line 1648)
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `find_blob_path` (Task 1), `_store.delete_by_file` (Task 4)
- Produces: `DELETE /files-api/blobs/<id>` → `204` / `404`

- [ ] **Step 1: Write the failing tests**

Add to `TestBlobAPI` in `tests/test_blobs.py`:

```python
    def _delete(self, path: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('DELETE', path)
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_delete_removes_file_and_cascades_annotations(self):
        _, blob = self._post_blob('del.md', b'x')
        self._post_annotation(blob['id'])

        status = self._delete(f'/files-api/blobs/{blob["id"]}')
        self.assertEqual(status, 204)

        get_status, _ = self._get(f'/files-api/blobs/{blob["id"]}')
        self.assertEqual(get_status, 404)
        _, remaining = self._get(f'/files-api/annotations?file={blob["id"]}')
        self.assertEqual(remaining, [])

    def test_delete_unknown_id_returns_404(self):
        status = self._delete('/files-api/blobs/deadbeef')
        self.assertEqual(status, 404)

    def test_deleting_same_blob_twice_returns_404_on_second_call(self):
        _, blob = self._post_blob('double-del.md', b'x')
        first_status = self._delete(f'/files-api/blobs/{blob["id"]}')
        second_status = self._delete(f'/files-api/blobs/{blob["id"]}')
        self.assertEqual(first_status, 204)
        self.assertEqual(second_status, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: falls through to the existing annotations-only `do_DELETE` logic, returns `404` with body `{'error': 'Not Found'}` for the wrong reason (path shape mismatch, not "blob not found") — `test_delete_unknown_id_returns_404` happens to pass already, but `test_delete_removes_file_and_cascades_annotations` and `test_deleting_same_blob_twice_returns_404_on_second_call` fail because nothing is actually deleted on the first call.

- [ ] **Step 3: Implement the `do_DELETE` blob branch**

In `file_share_serve.py`, find `do_DELETE` (currently ~line 1648):

```python
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip('/').split('/')
        if len(parts) != 3 or parts[:2] != ['files-api', 'annotations']:
            self._send_json(404, {'error': 'Not Found'})
            return

        ann_id = parts[2]
        deleted = _store.delete(ann_id)
        if deleted:
            self.send_response(204)
            self.end_headers()
        else:
            self._send_json(404, {'error': 'Annotation not found'})
```

Change to:

```python
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip('/').split('/')

        if len(parts) == 3 and parts[:2] == ['files-api', 'blobs']:
            blob_id = parts[2]
            fs_path = find_blob_path(blob_id)
            if fs_path is None:
                self._send_json(404, {'error': 'Blob not found'})
                return
            fs_path.unlink()
            _store.delete_by_file(blob_id)
            self.send_response(204)
            self.end_headers()
            return

        if len(parts) != 3 or parts[:2] != ['files-api', 'annotations']:
            self._send_json(404, {'error': 'Not Found'})
            return

        ann_id = parts[2]
        deleted = _store.delete(ann_id)
        if deleted:
            self.send_response(204)
            self.end_headers()
        else:
            self._send_json(404, {'error': 'Annotation not found'})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: add DELETE /files-api/blobs/<id> with cascading annotation cleanup"
```

---

### Task 7: Display layer — strip id prefix in directory listing, preview page, breadcrumb

**Files:**
- Modify: `file_share_serve.py` — `serve_directory`, `build_breadcrumb`, `serve_preview` methods (currently lines 1753-1918, exact line numbers will have shifted from Tasks 1-6's insertions — locate by method name and the shown code)
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `parse_blob_name` (Task 1)
- Produces: no new public interface — this only changes rendered HTML content, verified by fetching pages over HTTP and asserting on their body text.

- [ ] **Step 1: Write the failing tests**

Add to `TestBlobAPI` in `tests/test_blobs.py`:

```python
    def _get_html(self, path: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET', path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8', errors='replace')

    def test_directory_listing_hides_id_prefix(self):
        _, blob = self._post_blob('visible-name.md', b'# hi')
        status, html_body = self._get_html('/files/')
        self.assertEqual(status, 200)
        self.assertIn('visible-name.md', html_body)
        self.assertNotIn(f'{blob["id"]}-visible-name.md', html_body)

    def test_preview_page_shows_clean_filename_and_ids_for_annotations(self):
        _, blob = self._post_blob('preview-me.md', b'# Title')
        status, html_body = self._get_html(f'/files/{blob["id"]}-preview-me.md')
        self.assertEqual(status, 200)
        self.assertIn('preview-me.md', html_body)
        self.assertIn(f"ANN_FILE = '{blob['id']}'", html_body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: `test_directory_listing_hides_id_prefix` fails (id-prefixed name is shown in full); `test_preview_page_shows_clean_filename_and_ids_for_annotations` fails (`ANN_FILE` is currently set to the URL path, e.g. `/a1b2c3d4-preview-me.md`, not the bare id).

- [ ] **Step 3: Update `serve_directory`**

In `file_share_serve.py`, find the entry-rendering loop inside `serve_directory`:

```python
        for entry in entries:
            name = html.escape(entry.name)
            if entry.is_dir():
                href = url_path + name + '/'
                icon = '📁'
                size_str = '—'
            else:
                href = url_path + name
                icon = '📄'
                try:
                    size_str = format_size(entry.stat().st_size)
                except OSError:
                    size_str = '—'
            rows.append(f'<tr><td><span class="icon">{icon}</span><a href="{html.escape(href)}">{name}</a></td><td class="size">{size_str}</td></tr>')
```

Change to:

```python
        for entry in entries:
            parsed = parse_blob_name(entry.name) if entry.is_file() else None
            display_name = html.escape(parsed[1] if parsed else entry.name)
            if entry.is_dir():
                href = url_path + html.escape(entry.name) + '/'
                icon = '📁'
                size_str = '—'
            else:
                href = url_path + html.escape(entry.name)
                icon = '📄'
                try:
                    size_str = format_size(entry.stat().st_size)
                except OSError:
                    size_str = '—'
            rows.append(f'<tr><td><span class="icon">{icon}</span><a href="{href}">{display_name}</a></td><td class="size">{size_str}</td></tr>')
```

- [ ] **Step 4: Update `build_breadcrumb`**

Find `build_breadcrumb`:

```python
    def build_breadcrumb(self, url_path):
        """Build a breadcrumb trail for the URL path."""
        parts = url_path.strip('/').split('/')
        crumbs = [f'<a href="{PREFIX}/">/files/</a>']
        accumulated = PREFIX
        for i, part in enumerate(parts):
            accumulated += '/' + part
            if i == len(parts) - 1:
                crumbs.append(html.escape(part))
            else:
                crumbs.append(f'<a href="{html.escape(accumulated + "/")}">{html.escape(part)}/</a>')
        return ' '.join(crumbs)
```

Change the final-crumb branch to strip the blob id prefix for display:

```python
    def build_breadcrumb(self, url_path):
        """Build a breadcrumb trail for the URL path."""
        parts = url_path.strip('/').split('/')
        crumbs = [f'<a href="{PREFIX}/">/files/</a>']
        accumulated = PREFIX
        for i, part in enumerate(parts):
            accumulated += '/' + part
            if i == len(parts) - 1:
                parsed = parse_blob_name(part)
                display = parsed[1] if parsed else part
                crumbs.append(html.escape(display))
            else:
                crumbs.append(f'<a href="{html.escape(accumulated + "/")}">{html.escape(part)}/</a>')
        return ' '.join(crumbs)
```

- [ ] **Step 5: Update `serve_preview`**

Find the start of `serve_preview`:

```python
    def serve_preview(self, fs_path, url_path):
        stat = fs_path.stat()
        size = stat.st_size
        filename = fs_path.name
```

Change to:

```python
    def serve_preview(self, fs_path, url_path):
        stat = fs_path.stat()
        size = stat.st_size
        parsed_blob = parse_blob_name(fs_path.name)
        filename = parsed_blob[1] if parsed_blob else fs_path.name
```

Then find where `ann_file_key` is set, near the bottom of the method:

```python
        breadcrumb = self.build_breadcrumb(url_path)
        ann_file_key = url_path[len(PREFIX):]
```

Change to:

```python
        breadcrumb = self.build_breadcrumb(url_path)
        ann_file_key = parsed_blob[0] if parsed_blob else url_path[len(PREFIX):]
```

(No other changes needed in `serve_preview` — every other use of `filename` in that method already refers to the local variable, which now holds the clean display name.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_blobs -v`
Expected: all tests `ok`

- [ ] **Step 7: Run the full test suite**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest discover -s tests -v`
Expected: all tests `ok` (confirms Task 7's display changes didn't break the existing annotation tests, which also hit `serve_preview` indirectly is not the case — but this catches any regression across both files)

- [ ] **Step 8: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add file_share_serve.py tests/test_blobs.py
git commit -m "feat: strip blob id prefix from directory listing, breadcrumb, and preview page"
```

---

### Task 8: Migration script for the 23 existing flat files

**Files:**
- Create: `scripts/migrate_to_blobs.py`
- Test: `tests/test_migrate_to_blobs.py`

**Interfaces:**
- Consumes: none from earlier tasks (deliberately standalone — operates on real `SHARED_DIR`/`ANNOTATIONS_FILE`, not the helpers in `file_share_serve.py`, so it can be reviewed and re-run independently of server internals)
- Produces: `migrate(shared_dir: Path, annotations_file: Path) -> dict` — returns `{"renamed": {old_key: new_id, ...}, "remapped": <int>}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate_to_blobs.py`:

```python
"""Tests for the one-time flat-to-blob migration script."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))
import migrate_to_blobs as migrate_mod


class TestMigrateToBlobs(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name)
        self.annotations_file = self.shared / '.annotations.json'

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_renames_plain_files_to_blob_form(self):
        (self.shared / 'report.md').write_text('hello')
        self.annotations_file.write_text('[]')

        result = migrate_mod.migrate(self.shared, self.annotations_file)

        self.assertEqual(len(result['renamed']), 1)
        remaining = list(self.shared.glob('*.md'))
        self.assertEqual(len(remaining), 1)
        self.assertRegex(remaining[0].name, r'^[0-9a-f]{8}-report\.md$')

    def test_remaps_annotation_file_keys(self):
        (self.shared / 'notes.md').write_text('hello')
        self.annotations_file.write_text(json.dumps([
            {'id': 'ann-1', 'file': '/notes.md', 'selected_text': 'x',
             'offset_start': 0, 'offset_end': 1, 'type': 'upvote',
             'comment': '', 'author': 'hal', 'created_at': '2026-01-01T00:00:00Z'},
        ]))

        result = migrate_mod.migrate(self.shared, self.annotations_file)

        self.assertEqual(result['remapped'], 1)
        anns = json.loads(self.annotations_file.read_text())
        new_id = list(result['renamed'].values())[0]
        self.assertEqual(anns[0]['file'], new_id)

    def test_backs_up_annotations_file_before_writing(self):
        (self.shared / 'a.md').write_text('x')
        self.annotations_file.write_text(json.dumps([
            {'id': 'ann-1', 'file': '/a.md', 'selected_text': 'x',
             'offset_start': 0, 'offset_end': 1, 'type': 'upvote',
             'comment': '', 'author': 'hal', 'created_at': '2026-01-01T00:00:00Z'},
        ]))
        original_contents = self.annotations_file.read_text()

        migrate_mod.migrate(self.shared, self.annotations_file)

        backup = self.annotations_file.with_suffix('.json.bak')
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(), original_contents)

    def test_is_idempotent(self):
        (self.shared / 'once.md').write_text('x')
        self.annotations_file.write_text('[]')

        first = migrate_mod.migrate(self.shared, self.annotations_file)
        second = migrate_mod.migrate(self.shared, self.annotations_file)

        self.assertEqual(len(first['renamed']), 1)
        self.assertEqual(len(second['renamed']), 0)

    def test_ignores_annotations_json_itself(self):
        self.annotations_file.write_text('[]')
        migrate_mod.migrate(self.shared, self.annotations_file)
        self.assertTrue(self.annotations_file.exists())


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_migrate_to_blobs -v`
Expected: `ModuleNotFoundError: No module named 'migrate_to_blobs'`

- [ ] **Step 3: Write the migration script**

Create `scripts/migrate_to_blobs.py`:

```python
#!/usr/bin/env python3
"""One-time migration: rename flat shared/ files to <id>-<filename> blobs
and remap .annotations.json file keys from path strings to blob ids.

Safe to re-run: files already in <id>-<filename> form and annotations
already keyed by a bare 8-hex id are left untouched.

Usage: python3 scripts/migrate_to_blobs.py
"""
import json
import re
import uuid
from pathlib import Path

DEFAULT_SHARED_DIR = Path('/home/halr9000/shared')

BLOB_NAME_RE = re.compile(r'^[0-9a-f]{8}-.+$')
BLOB_ID_RE = re.compile(r'^[0-9a-f]{8}$')


def migrate(shared_dir: Path, annotations_file: Path) -> dict:
    """Migrate shared_dir's flat files to blob form and remap annotations_file.

    Returns {"renamed": {old_share_relative_path: new_blob_id, ...}, "remapped": int}.
    """
    existing_ids = {
        p.name.split('-', 1)[0] for p in shared_dir.iterdir()
        if p.is_file() and BLOB_NAME_RE.match(p.name)
    }

    renamed = {}
    for p in sorted(shared_dir.iterdir()):
        if not p.is_file() or p.name == annotations_file.name:
            continue
        if BLOB_NAME_RE.match(p.name):
            continue  # already migrated

        blob_id = uuid.uuid4().hex[:8]
        while blob_id in existing_ids:
            blob_id = uuid.uuid4().hex[:8]
        existing_ids.add(blob_id)

        old_key = f'/{p.name}'
        new_path = p.with_name(f'{blob_id}-{p.name}')
        p.rename(new_path)
        renamed[old_key] = blob_id
        print(f'{old_key} -> {new_path.name}')

    remapped = 0
    if renamed and annotations_file.exists():
        backup = annotations_file.with_suffix('.json.bak')
        backup.write_text(annotations_file.read_text())
        print(f'Backed up annotations to {backup}')

        anns = json.loads(annotations_file.read_text())
        for a in anns:
            if BLOB_ID_RE.match(a['file']):
                continue  # already migrated
            if a['file'] in renamed:
                a['file'] = renamed[a['file']]
                remapped += 1
        annotations_file.write_text(json.dumps(anns, indent=2, ensure_ascii=False))
        print(f'Remapped {remapped} of {len(anns)} annotation file keys.')

    if not renamed:
        print('No files to migrate.')

    return {'renamed': renamed, 'remapped': remapped}


if __name__ == '__main__':
    annotations_file = DEFAULT_SHARED_DIR / '.annotations.json'
    migrate(DEFAULT_SHARED_DIR, annotations_file)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/halr9000/.openclaw/workspace/projects/file-share && python3 -m unittest tests.test_migrate_to_blobs -v`
Expected: all tests `ok`

- [ ] **Step 5: Commit the script**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add scripts/migrate_to_blobs.py tests/test_migrate_to_blobs.py
git commit -m "feat: add migration script for flat files to blob form"
```

- [ ] **Step 6: Run the migration against the live shared/ directory**

```bash
python3 /home/halr9000/.openclaw/workspace/projects/file-share/scripts/migrate_to_blobs.py
```

Expected: 23 `renamed` lines printed, `Remapped 15 of 15 annotation file keys.`

- [ ] **Step 7: Restart the service and verify**

```bash
systemctl --user restart file-share.service
sleep 1
systemctl --user is-active file-share.service
curl -s "https://neo.taileffc7.ts.net/files-api/blobs" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
```

Expected: `active`, then `23`

---

### Task 9: Rewrite `SKILL.md`

**Files:**
- Modify: `/home/halr9000/.openclaw/workspace/skills/file-share/SKILL.md` (full rewrite)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Replace the entire file**

Write `/home/halr9000/.openclaw/workspace/skills/file-share/SKILL.md`:

```markdown
---
name: file-share
description: >
  Use when: sharing a file with Hal's devices, generating a share URL, adding,
  replacing, or removing a file from the shared object store, listing what's
  currently shared, reading or summarizing Hal's annotations/feedback/comments
  on a shared doc, or revising a doc based on his annotations. Triggers on:
  "share this file", "give me a link to", "what files are shared", "remove
  from shared", "Hal left feedback on", "address the comments on", "drain
  annotations", "revise based on his annotations".
---

# file-share

Serves `/home/halr9000/shared/` to Hal's Tailscale network (tailnet) as a flat,
object-store-style blob store at:

```
https://neo.taileffc7.ts.net/files/
```

Only reachable on the tailnet — not publicly accessible.

## Architecture

- **File server**: Python stdlib `http.server` (`ThreadingHTTPServer`) → `localhost:3458`
- **Systemd service**: `file-share.service` (user service, enabled, auto-restart)
- **Proxy routes**: `tailscale-proxy.js` routes `/files/*` and `/files-api/*` → port 3458
- **TLS**: Tailscale Serve owns port 443; handles HTTPS automatically
- **Source**: `workspace/projects/file-share/` (own git repo — `file_share_serve.py` + tests)

## Storage model — flat blob store, not a filesystem

`shared/` has **no subdirectories** and never should. Every file is stored as
`<id>-<filename>` (an 8-hex-char id, a hyphen, then the original filename),
which is what lets two files share the same `filename` while staying distinct.
`id` is the file's real identity — use it (not the filename) whenever you need
to reference a specific file (e.g. as the annotation lookup key).

**Never write directly into `shared/` with `cp`/`mv`/`echo >`.** The only
sanctioned way to add, replace, or remove a file is the CRUD API below — it's
what assigns the id, validates the filename can't create a nested path, and
keeps annotations in sync. A stray `mkdir` or `cp` bypasses all of that.

## Jeeves operations

### Add a file and get its URL

```bash
curl -s -X POST --data-binary @/path/to/file.ext \
  "https://neo.taileffc7.ts.net/files-api/blobs?filename=file.ext"
```

Returns:
```json
{"id": "a1b2c3d4", "filename": "file.ext", "url": "/files/a1b2c3d4-file.ext",
 "size": 1234, "mime": "text/plain", "created_at": "2026-07-02T14:00:00+00:00"}
```

The share URL is `https://neo.taileffc7.ts.net` + `url` from the response —
e.g. `https://neo.taileffc7.ts.net/files/a1b2c3d4-file.ext`.

### List all shared files

```bash
curl -s "https://neo.taileffc7.ts.net/files-api/blobs" | jq .
```

Or browse the directory index (shows clean filenames, ids hidden):
```
https://neo.taileffc7.ts.net/files/
```

### Look up one file's metadata by id

```bash
curl -s "https://neo.taileffc7.ts.net/files-api/blobs/<id>" | jq .
```

### Replace a file's content (same URL, auto-drains its annotations)

```bash
curl -s -X PUT --data-binary @/path/to/new-version.ext \
  "https://neo.taileffc7.ts.net/files-api/blobs/<id>"
```

Same `id`, same URL — Hal's existing link keeps working and now shows the new
content. Any annotations on the old content are deleted server-side as part of
this call (`drained_annotations` in the response tells you how many). **You
still have to write the "Annotation Log" section into the new content
yourself before calling PUT** — see the drain protocol below; the server only
handles clearing the stale entries, not summarizing them.

### Remove a shared file

```bash
curl -s -X DELETE "https://neo.taileffc7.ts.net/files-api/blobs/<id>"
```

Removes the file and cascades deletion of any annotations on it.

## Service management

```bash
# Status
systemctl --user status file-share.service

# Restart (e.g. after code changes)
systemctl --user restart file-share.service

# Logs
journalctl --user -u file-share.service -n 50
```

## Preview vs. raw URLs

By default, file URLs serve a **GitHub Gist-style HTML preview page** with:
- Syntax highlighting (highlight.js) for code/XML/JSON/etc.
- Rendered markdown for `.md` files
- Inline display for images (`.jpg`, `.png`, `.gif`, `.webp`, `.svg`)
- HTML5 audio player for `.mp3`, `.wav`, `.ogg`, etc.
- HTML5 video player for `.mp4`, `.webm`, etc.
- "Raw", "Copy", and "Download" buttons in the top bar

To get the **raw file** (direct download / `Content-Disposition: attachment`), append `?raw=1`:

```
# Preview page (default):
https://neo.taileffc7.ts.net/files/a1b2c3d4-send-to-jeeves.tsk.xml

# Raw download:
https://neo.taileffc7.ts.net/files/a1b2c3d4-send-to-jeeves.tsk.xml?raw=1
```

When sharing a file URL with Hal, use the plain URL (preview page). If he needs a
direct download link, append `?raw=1`.

## Security notes

- Files are served **only** on the Tailscale interface — not on the public internet
- Do NOT put files containing secrets (API keys, tokens, passwords) here unless
  Hal explicitly asks and understands the risk (tailnet devices can all read these)
- `POST`/`PUT`/`DELETE` on `/files-api/blobs` have no auth beyond Tailscale network
  membership — same trust model as the annotation API. Any tailnet device can
  create/replace/remove files, not just Jeeves.
- `GET` requests are protected against path traversal (`.resolve()` +
  `.relative_to()` check, returns 403 on escape); uploaded filenames are
  validated the same way on write (reject `/`, `..`, NUL, leading `.`)

## Annotations

Annotations are stored at `/home/halr9000/shared/.annotations.json`. Each entry:

````json
{
  "id": "uuid",
  "file": "a1b2c3d4",
  "selected_text": "the annotated text",
  "offset_start": 0,
  "offset_end": 11,
  "type": "upvote | downvote | comment",
  "comment": "optional comment body",
  "author": "hal",
  "created_at": "2026-06-10T19:00:00Z"
}
````

`file` is the blob **id** (not a path — see Storage model above).

### Jeeves: read all annotations for a file

````bash
curl -s "https://neo.taileffc7.ts.net/files-api/annotations?file=<id>" | jq .
````

### Jeeves: summarize annotations on a shared doc

When Hal asks you to review a shared document with annotations:
1. Fetch the file: `curl -s "https://neo.taileffc7.ts.net/files/<id>?raw=1"`
2. Fetch its annotations: `GET /files-api/annotations?file=<id>`
3. Cross-reference `selected_text` + `offset_start/end` with the file content
4. Surface upvotes (sections Hal likes), downvotes (sections to revise), comments (specific feedback)

### Jeeves: rewrite a doc that has annotations (drain protocol)

**REQUIRED before calling PUT on any file that has annotations.** The offsets
in each annotation only make sense against the content they were made on —
once you replace the content, they're stale, and PUT clears them
automatically. If you don't preserve them somewhere first, Hal's feedback is
gone.

1. **Fetch current annotations** for this blob id:

    ```bash
    curl -s "https://neo.taileffc7.ts.net/files-api/annotations?file=<id>" | jq .
    ```

2. **Compose the new document content with an `## Annotation Log` appended**
   (before the final newline, or after the last content section). Format:

    ```markdown
    ## Annotation Log
    *(Auto-generated from annotations present before this revision — cleared on PUT)*

    | # | Type | Selected text | Comment | Addressed? |
    |---|------|---------------|---------|------------|
    | 1 | upvote | "REQUIRED SUB-SKILL:" | — | Retained |
    | 2 | downvote | "checkbox" | — | Removed |
    | 3 | comment | "Architecture" | add a thingy here | See new Architecture section |
    ```

    Include every annotation. For `type=comment`, carry the `comment` text
    verbatim. The "Addressed?" column is Jeeves' judgment call.

3. **PUT the new content**:

    ```bash
    curl -s -X PUT --data-binary @new-version.md \
      "https://neo.taileffc7.ts.net/files-api/blobs/<id>"
    ```

    The response's `drained_annotations` count should match what you saw in
    step 1 — if it doesn't, something raced (e.g. Hal added a new annotation
    between steps 1 and 3); re-check before telling Hal you're done.

#### Revising vs. replacing

`PUT` always replaces content at the same id/URL. If you want to keep the old
version around instead of overwriting it (e.g. Hal is mid-review and you want
him to be able to compare), create a **new** blob instead of PUTting — tell
him the new URL, leave the old one and its annotations untouched.

## Common mistakes

- **Writing into `shared/` with `cp`/`mv` instead of the API.** This skips id
  assignment, can silently collide with another file's name, and won't be
  visible to `GET /files-api/blobs`. Always use the API.
- **Creating a subdirectory.** `shared/` is flat by design — use a descriptive
  filename, not a category folder. The API's filename validation rejects `/`
  anyway, but don't work around it by shelling out to `mkdir` + `mv`.
- **Confusing the blob id with the filename.** Annotation lookups, `PUT`, and
  `DELETE` all key on the id (`a1b2c3d4`), not the filename (`report.md`) —
  two files can have the same filename but never the same id.
- **Skipping the drain protocol.** Calling `PUT` without first writing an
  Annotation Log into the new content silently discards Hal's feedback — the
  annotations are gone from the store the moment `PUT` succeeds.
- **Forgetting `?raw=1`** when Hal needs an actual download link instead of
  the preview page.
```

- [ ] **Step 2: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git add -A
git status
```

(SKILL.md lives outside this repo, under `workspace/skills/file-share/` — it has no git repo of its own to commit to. No commit action needed for this step; just verify the write succeeded by re-reading the file.)

---

### Task 10: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
python3 -m unittest discover -s tests -v
```

Expected: every test across `test_annotations.py`, `test_blobs.py`, `test_migrate_to_blobs.py` reports `ok`.

- [ ] **Step 2: Confirm the live service is on the current code and passing a smoke test**

```bash
systemctl --user restart file-share.service
sleep 1
systemctl --user is-active file-share.service
curl -s -X POST --data-binary 'smoke test' "https://neo.taileffc7.ts.net/files-api/blobs?filename=smoke-test.txt"
```

Expected: `active`, then a `201` JSON body with a fresh `id`.

- [ ] **Step 3: Clean up the smoke-test blob**

```bash
BLOB_ID=$(curl -s "https://neo.taileffc7.ts.net/files-api/blobs" | python3 -c "import json,sys; print([b['id'] for b in json.load(sys.stdin) if b['filename']=='smoke-test.txt'][0])")
curl -s -X DELETE "https://neo.taileffc7.ts.net/files-api/blobs/$BLOB_ID"
```

Expected: `204`, and the file no longer appears in `GET /files-api/blobs`.

- [ ] **Step 4: Final commit**

```bash
cd /home/halr9000/.openclaw/workspace/projects/file-share
git log --oneline
git status
```

Expected: clean working tree, one commit per task above.
