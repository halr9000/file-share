# File-Share Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add text annotation to the file-share preview server so Hal can select text and upvote/downvote/comment, annotations persist on the server, and Jeeves can read them to support collaborative documentation review.

**Architecture:** The server (`scratch/file-share-serve.py`) gains a thread-safe `AnnotationStore` class that reads/writes `/home/halr9000/shared/.annotations.json`, plus two new endpoints (`POST /files-api/annotations` and `GET /files-api/annotations`). The existing `PREVIEW_HTML_TEMPLATE` gains JS that (1) listens for text selection via both `mouseup` and `selectionchange` (mobile-compatible), (2) slides a **fixed bottom action bar** up from the bottom of the screen with 👍 👎 💬 buttons — no floating popup that a finger would occlude, (3) loads annotations on page load and highlights them using the CSS Custom Highlight API (no DOM mutation, no conflict with syntax highlighting), and (4) shows annotations in a **bottom sheet panel** triggered by a fixed bottom-right FAB badge.

**Tech Stack:** Python 3 stdlib (threading, json, uuid, datetime) for backend; Vanilla JS + CSS Custom Highlight API (Chrome 105+, Firefox 131+) for frontend; `unittest` + `http.client` for tests.

---

## File Map

| File | Status | Responsibility |
|------|--------|----------------|
| `scratch/file-share-serve.py` | Modify | AnnotationStore class, API endpoints, frontend template changes |
| `scratch/tests/test_annotations.py` | Create | Unit tests for AnnotationStore and API endpoints |

---

### Task 1: Test skeleton + AnnotationStore tests

**Files:**
- Create: `scratch/tests/__init__.py`
- Create: `scratch/tests/test_annotations.py`

The tests spin up a real `ThreadingHTTPServer` on a random port and make live HTTP requests. No mocking of the handler.

- [ ] **Step 1: Create test directory**

```bash
mkdir -p /home/halr9000/.openclaw/workspace/scratch/tests
touch /home/halr9000/.openclaw/workspace/scratch/tests/__init__.py
```

- [ ] **Step 2: Write failing tests for AnnotationStore**

Create `scratch/tests/test_annotations.py`:

```python
"""Tests for file-share annotation feature."""
import json
import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent))
import file_share_serve as fss


class TestAnnotationStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmpdir.name) / '.annotations.json'
        self.store = fss.AnnotationStore(self.store_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_add_annotation_returns_dict_with_id(self):
        ann = self.store.add('/docs/file.md', 'hello world', 0, 11, 'upvote', '', 'hal')
        self.assertIn('id', ann)
        self.assertEqual(ann['file'], '/docs/file.md')
        self.assertEqual(ann['selected_text'], 'hello world')
        self.assertEqual(ann['type'], 'upvote')

    def test_add_annotation_persists_to_json(self):
        self.store.add('/docs/file.md', 'hello', 0, 5, 'downvote', '', 'hal')
        data = json.loads(self.store_path.read_text())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['type'], 'downvote')

    def test_get_returns_annotations_for_file(self):
        self.store.add('/docs/a.md', 'foo', 0, 3, 'upvote', '', 'hal')
        self.store.add('/docs/b.md', 'bar', 0, 3, 'comment', 'nice', 'hal')
        result = self.store.get('/docs/a.md')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['selected_text'], 'foo')

    def test_get_all_returns_all_annotations(self):
        self.store.add('/docs/a.md', 'foo', 0, 3, 'upvote', '', 'hal')
        self.store.add('/docs/b.md', 'bar', 0, 3, 'comment', 'nice', 'hal')
        result = self.store.get_all()
        self.assertEqual(len(result), 2)

    def test_delete_removes_annotation(self):
        ann = self.store.add('/docs/a.md', 'foo', 0, 3, 'upvote', '', 'hal')
        ann_id = ann['id']
        deleted = self.store.delete(ann_id)
        self.assertTrue(deleted)
        self.assertEqual(len(self.store.get('/docs/a.md')), 0)

    def test_delete_nonexistent_returns_false(self):
        result = self.store.delete('no-such-id')
        self.assertFalse(result)

    def test_store_loads_existing_file_on_init(self):
        # Write a pre-existing annotations file
        pre = [{'id': 'abc', 'file': '/f.md', 'selected_text': 'x',
                 'offset_start': 0, 'offset_end': 1, 'type': 'upvote',
                 'comment': '', 'author': 'hal', 'created_at': '2026-01-01T00:00:00Z'}]
        self.store_path.write_text(json.dumps(pre))
        store2 = fss.AnnotationStore(self.store_path)
        self.assertEqual(len(store2.get('/f.md')), 1)

    def test_thread_safety_concurrent_writes(self):
        errors = []
        def write():
            try:
                for i in range(10):
                    self.store.add('/f.md', 'x', i, i+1, 'upvote', '', 'hal')
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=write) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.get('/f.md')), 50)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 -m pytest tests/test_annotations.py -v 2>&1 | head -30
```

Expected: `ImportError` or `AttributeError: module 'file_share_serve' has no attribute 'AnnotationStore'`

Note: `file-share-serve.py` contains a hyphen so Python can't import it with `import`. Rename first:

```bash
cp /home/halr9000/.openclaw/workspace/scratch/file-share-serve.py \
   /home/halr9000/.openclaw/workspace/scratch/file_share_serve.py
```

Run again — should fail with `AttributeError: module 'file_share_serve' has no attribute 'AnnotationStore'`.

---

### Task 2: Implement AnnotationStore

**Files:**
- Modify: `scratch/file_share_serve.py` — add `AnnotationStore` class after imports, add global `_store` instance

- [ ] **Step 1: Add imports at the top of file_share_serve.py**

Add after the existing imports (after `from pathlib import Path`):

```python
import json
import threading
import uuid
from datetime import datetime, timezone
```

(Note: `json`, `os`, `sys`, `html`, `mimetypes`, `urllib` are already imported — only add what's missing.)

- [ ] **Step 2: Add AnnotationStore class**

Add this class after the existing constants (after the `TEXT_MIMES` block, before `def format_size`):

```python
ANNOTATIONS_FILE = SHARED_DIR / '.annotations.json'


class AnnotationStore:
    def __init__(self, path: Path = ANNOTATIONS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data: list[dict] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = []

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))

    def add(self, file: str, selected_text: str, offset_start: int, offset_end: int,
            ann_type: str, comment: str, author: str) -> dict:
        ann = {
            'id': str(uuid.uuid4()),
            'file': file,
            'selected_text': selected_text,
            'offset_start': offset_start,
            'offset_end': offset_end,
            'type': ann_type,
            'comment': comment,
            'author': author,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._data.append(ann)
            self._save()
        return ann

    def get(self, file: str) -> list[dict]:
        with self._lock:
            return [a for a in self._data if a['file'] == file]

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._data)

    def delete(self, ann_id: str) -> bool:
        with self._lock:
            original_len = len(self._data)
            self._data = [a for a in self._data if a['id'] != ann_id]
            if len(self._data) < original_len:
                self._save()
                return True
            return False


_store = AnnotationStore()
```

- [ ] **Step 3: Run AnnotationStore tests**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 -m pytest tests/test_annotations.py -v -k "TestAnnotationStore"
```

Expected: All `TestAnnotationStore` tests pass.

- [ ] **Step 4: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
git init . 2>/dev/null || true
git add file_share_serve.py tests/
git commit -m "feat(file-share): add AnnotationStore with thread-safe JSON persistence"
```

---

### Task 3: Test + implement annotation API endpoints

**Files:**
- Modify: `scratch/tests/test_annotations.py` — add `TestAnnotationAPI` class
- Modify: `scratch/file_share_serve.py` — add `do_POST`, update `do_GET` for `/files-api/` prefix

- [ ] **Step 1: Add API tests to test_annotations.py**

Append this class to the test file (before `if __name__ == '__main__'`):

```python
class TestAnnotationAPI(unittest.TestCase):
    """Integration tests: spins up a real server, hits it with http.client."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        fss.SHARED_DIR = Path(cls.tmpdir.name)
        fss.ANNOTATIONS_FILE = fss.SHARED_DIR / '.annotations.json'
        fss._store = fss.AnnotationStore(fss.ANNOTATIONS_FILE)
        # Create a dummy shared file so previews work
        (fss.SHARED_DIR / 'test.md').write_text('# Hello\nworld')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), fss.GistHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmpdir.cleanup()

    def _post(self, body: dict):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        payload = json.dumps(body).encode()
        conn.request('POST', '/files-api/annotations',
                     body=payload,
                     headers={'Content-Type': 'application/json',
                               'Content-Length': str(len(payload))})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if data else {}

    def _get(self, file_path: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET', f'/files-api/annotations?file={urllib.parse.quote(file_path)}')
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def _delete(self, ann_id: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('DELETE', f'/files-api/annotations/{ann_id}')
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_post_creates_annotation(self):
        status, body = self._post({
            'file': '/test.md',
            'selected_text': 'Hello',
            'offset_start': 2,
            'offset_end': 7,
            'type': 'upvote',
            'comment': '',
            'author': 'hal',
        })
        self.assertEqual(status, 201)
        self.assertIn('id', body)
        self.assertEqual(body['type'], 'upvote')

    def test_post_validates_required_fields(self):
        status, body = self._post({'file': '/test.md'})
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_post_validates_type_values(self):
        status, body = self._post({
            'file': '/test.md', 'selected_text': 'x',
            'offset_start': 0, 'offset_end': 1,
            'type': 'invalid', 'comment': '', 'author': 'hal',
        })
        self.assertEqual(status, 400)

    def test_get_returns_annotations_for_file(self):
        self._post({'file': '/get-test.md', 'selected_text': 'foo',
                    'offset_start': 0, 'offset_end': 3,
                    'type': 'comment', 'comment': 'great point', 'author': 'hal'})
        status, body = self._get('/get-test.md')
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertTrue(any(a['file'] == '/get-test.md' for a in body))

    def test_get_empty_for_unannotated_file(self):
        status, body = self._get('/no-annotations-here.md')
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    def test_delete_removes_annotation(self):
        _, ann = self._post({'file': '/del-test.md', 'selected_text': 'bye',
                              'offset_start': 0, 'offset_end': 3,
                              'type': 'downvote', 'comment': '', 'author': 'hal'})
        status = self._delete(ann['id'])
        self.assertEqual(status, 204)
        _, remaining = self._get('/del-test.md')
        self.assertFalse(any(a['id'] == ann['id'] for a in remaining))

    def test_delete_nonexistent_returns_404(self):
        status = self._delete('no-such-id')
        self.assertEqual(status, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 -m pytest tests/test_annotations.py -v -k "TestAnnotationAPI" 2>&1 | head -30
```

Expected: Connection refused or 404s — `do_POST` and `/files-api/` routes don't exist yet.

- [ ] **Step 3: Add do_POST and do_DELETE to GistHandler**

In `file_share_serve.py`, add these methods to the `GistHandler` class (after `do_HEAD`):

```python
    def _send_json(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return None

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/files-api/annotations':
            self._send_json(404, {'error': 'Not Found'})
            return

        body = self._read_json_body()
        if body is None:
            self._send_json(400, {'error': 'Invalid JSON'})
            return

        required = {'file', 'selected_text', 'offset_start', 'offset_end', 'type'}
        missing = required - body.keys()
        if missing:
            self._send_json(400, {'error': f'Missing fields: {sorted(missing)}'})
            return

        if body['type'] not in ('upvote', 'downvote', 'comment'):
            self._send_json(400, {'error': 'type must be upvote, downvote, or comment'})
            return

        ann = _store.add(
            file=str(body['file']),
            selected_text=str(body['selected_text']),
            offset_start=int(body['offset_start']),
            offset_end=int(body['offset_end']),
            ann_type=str(body['type']),
            comment=str(body.get('comment', '')),
            author=str(body.get('author', 'hal')),
        )
        self._send_json(201, ann)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        # Expect /files-api/annotations/<id>
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

- [ ] **Step 4: Update do_GET to handle /files-api/annotations**

At the top of the existing `do_GET` method, add a branch before the existing `PREFIX` check:

```python
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        url_path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        # Annotation API
        if url_path == '/files-api/annotations':
            file_param = query.get('file', [None])[0]
            if file_param is None:
                self._send_json(400, {'error': 'Missing ?file= parameter'})
                return
            self._send_json(200, _store.get(file_param))
            return

        # ... rest of existing do_GET unchanged ...
```

- [ ] **Step 5: Run all API tests**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 -m pytest tests/test_annotations.py -v
```

Expected: All tests pass (both `TestAnnotationStore` and `TestAnnotationAPI`).

- [ ] **Step 6: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
git add file_share_serve.py tests/test_annotations.py
git commit -m "feat(file-share): add annotation API endpoints (POST/GET/DELETE /files-api/annotations)"
```

---

### Task 4: Frontend — load and display existing annotations

**Files:**
- Modify: `scratch/file_share_serve.py` — update `PREVIEW_HTML_TEMPLATE` to add annotation CSS + load JS

Annotations are displayed using the **CSS Custom Highlight API** (`CSS.highlights`, `Highlight`, `Range`), which overlays color without touching the DOM or conflicting with syntax highlighting. The badge is a fixed bottom-right FAB; the panel is a full-width bottom sheet (works identically on desktop and mobile).

- [ ] **Step 1: Add annotation styles to PREVIEW_HTML_TEMPLATE**

In the `<style>` block of `PREVIEW_HTML_TEMPLATE`, add before the closing `</style>`:

```css
    /* Annotation highlights via CSS Custom Highlight API */
    ::highlight(ann-upvote)   {{ background-color: rgba(46, 160, 67, 0.35); }}
    ::highlight(ann-downvote) {{ background-color: rgba(248, 81, 73, 0.30); }}
    ::highlight(ann-comment)  {{ background-color: rgba(88, 166, 255, 0.30); }}

    /* FAB badge — fixed bottom-right, thumb-reachable */
    #ann-fab {{
      position: fixed;
      bottom: 24px;
      right: 20px;
      min-width: 48px;
      height: 48px;
      padding: 0 14px;
      background: #21262d;
      border: 1px solid #30363d;
      border-radius: 24px;
      font-size: 13px;
      color: #8b949e;
      cursor: pointer;
      z-index: 100;
      display: flex;
      align-items: center;
      gap: 6px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.4);
      transition: background 0.15s;
      -webkit-tap-highlight-color: transparent;
    }}
    #ann-fab:hover, #ann-fab:active {{ background: #30363d; }}

    /* Bottom sheet — shared by panel and comment entry */
    .ann-sheet {{
      position: fixed;
      bottom: 0; left: 0; right: 0;
      background: #161b22;
      border-top: 1px solid #30363d;
      border-radius: 16px 16px 0 0;
      z-index: 200;
      transform: translateY(100%);
      transition: transform 0.25s cubic-bezier(0.4,0,0.2,1);
      max-height: 70vh;
      display: flex;
      flex-direction: column;
    }}
    .ann-sheet.open {{ transform: translateY(0); }}
    .ann-sheet-handle {{
      width: 36px; height: 4px;
      background: #30363d;
      border-radius: 2px;
      margin: 10px auto 0;
      flex-shrink: 0;
    }}
    .ann-sheet-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px 8px;
      color: #e6edf3;
      font-size: 14px;
      font-weight: 600;
      flex-shrink: 0;
    }}
    .ann-sheet-close {{
      background: none; border: none; color: #484f58;
      font-size: 20px; cursor: pointer; padding: 4px 8px;
      min-width: 44px; min-height: 44px;
      display: flex; align-items: center; justify-content: center;
      -webkit-tap-highlight-color: transparent;
    }}
    .ann-sheet-body {{
      overflow-y: auto;
      padding: 0 16px 24px;
      flex: 1;
    }}

    /* Annotation list items */
    .ann-item {{
      border-bottom: 1px solid #21262d;
      padding: 10px 0;
      font-size: 13px;
      color: #c9d1d9;
    }}
    .ann-item:last-child {{ border-bottom: none; }}
    .ann-item-text {{
      color: #8b949e;
      font-style: italic;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-bottom: 2px;
    }}
    .ann-item-meta {{
      display: flex; gap: 8px; align-items: center;
    }}
    .ann-type-upvote   {{ color: #3fb950; }}
    .ann-type-downvote {{ color: #f85149; }}
    .ann-type-comment  {{ color: #58a6ff; }}
    .ann-item-delete {{
      margin-left: auto; cursor: pointer;
      color: #484f58; font-size: 12px;
      min-width: 44px; min-height: 44px;
      display: flex; align-items: center; justify-content: flex-end;
      -webkit-tap-highlight-color: transparent;
    }}
    .ann-item-delete:hover, .ann-item-delete:active {{ color: #f85149; }}

    /* Backdrop for sheets */
    #ann-backdrop {{
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.5);
      z-index: 190;
      display: none;
    }}
    #ann-backdrop.visible {{ display: block; }}
```

- [ ] **Step 2: Add annotation load + highlight JS to PREVIEW_HTML_TEMPLATE**

Add this `<script>` block just before the closing `</body>` tag:

```javascript
  <script>
    // ── Annotation system ────────────────────────────────────────────────────

    const ANN_FILE = '{ann_file_key}';

    const supportsHighlightAPI = typeof CSS !== 'undefined' && CSS.highlights;
    const highlights = {{ upvote: null, downvote: null, comment: null }};
    if (supportsHighlightAPI) {{
      highlights.upvote   = new Highlight();
      highlights.downvote = new Highlight();
      highlights.comment  = new Highlight();
      CSS.highlights.set('ann-upvote',   highlights.upvote);
      CSS.highlights.set('ann-downvote', highlights.downvote);
      CSS.highlights.set('ann-comment',  highlights.comment);
    }}

    let _annotations = [];

    function getPreviewTextNodes() {{
      const root = document.querySelector('.file-box pre code, .file-box .markdown-body, .file-box pre');
      if (!root) return [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      let node;
      while ((node = walker.nextNode())) nodes.push(node);
      return nodes;
    }}

    function resolveOffset(textNodes, charOffset) {{
      let remaining = charOffset;
      for (const node of textNodes) {{
        const len = node.textContent.length;
        if (remaining <= len) return {{ node, offset: remaining }};
        remaining -= len;
      }}
      const last = textNodes[textNodes.length - 1];
      return {{ node: last, offset: last ? last.textContent.length : 0 }};
    }}

    function applyHighlights(annotations) {{
      if (!supportsHighlightAPI) return;
      highlights.upvote.clear();
      highlights.downvote.clear();
      highlights.comment.clear();
      const textNodes = getPreviewTextNodes();
      if (!textNodes.length) return;
      for (const ann of annotations) {{
        try {{
          const start = resolveOffset(textNodes, ann.offset_start);
          const end   = resolveOffset(textNodes, ann.offset_end);
          const range = new Range();
          range.setStart(start.node, start.offset);
          range.setEnd(end.node, end.offset);
          if (highlights[ann.type]) highlights[ann.type].add(range);
        }} catch (e) {{ /* offset out of range — skip */ }}
      }}
    }}

    function renderFab(annotations) {{
      const fab = document.getElementById('ann-fab');
      if (!fab) return;
      const counts = {{ upvote: 0, downvote: 0, comment: 0 }};
      for (const a of annotations) counts[a.type] = (counts[a.type] || 0) + 1;
      const total = counts.upvote + counts.downvote + counts.comment;
      if (!total) {{
        fab.innerHTML = '💬';
        return;
      }}
      const parts = [];
      if (counts.upvote)   parts.push(`<span class="ann-type-upvote">👍${{counts.upvote}}</span>`);
      if (counts.downvote) parts.push(`<span class="ann-type-downvote">👎${{counts.downvote}}</span>`);
      if (counts.comment)  parts.push(`<span class="ann-type-comment">💬${{counts.comment}}</span>`);
      fab.innerHTML = parts.join(' ');
    }}

    function renderPanel(annotations) {{
      const body = document.getElementById('ann-panel-body');
      if (!body) return;
      const icons = {{ upvote: '👍', downvote: '👎', comment: '💬' }};
      if (!annotations.length) {{
        body.innerHTML = '<div style="color:#484f58;text-align:center;padding:20px 0">No annotations yet</div>';
        return;
      }}
      body.innerHTML = annotations.map(a => `
        <div class="ann-item">
          <div class="ann-item-text">"${{a.selected_text.slice(0,80)}}${{a.selected_text.length>80?'…':''}}"</div>
          <div class="ann-item-meta">
            <span class="ann-type-${{a.type}}">${{icons[a.type]}} ${{a.type}}</span>
            <span style="color:#484f58;font-size:11px">${{a.author}}</span>
            ${{a.comment ? `<span style="color:#c9d1d9;font-size:12px">— ${{a.comment}}</span>` : ''}}
            <span class="ann-item-delete" onclick="deleteAnnotation('${{a.id}}')">✕</span>
          </div>
        </div>`).join('');
    }}

    async function loadAnnotations() {{
      try {{
        const resp = await fetch(`/files-api/annotations?file=${{encodeURIComponent(ANN_FILE)}}`);
        if (!resp.ok) return;
        _annotations = await resp.json();
        applyHighlights(_annotations);
        renderFab(_annotations);
        renderPanel(_annotations);
      }} catch (e) {{ console.error('Failed to load annotations', e); }}
    }}

    async function deleteAnnotation(annId) {{
      const resp = await fetch(`/files-api/annotations/${{annId}}`, {{ method: 'DELETE' }});
      if (resp.ok) loadAnnotations();
    }}

    // ── Sheet helpers ─────────────────────────────────────────────────────────

    function openSheet(id) {{
      document.getElementById(id).classList.add('open');
      document.getElementById('ann-backdrop').classList.add('visible');
    }}
    function closeSheet(id) {{
      document.getElementById(id).classList.remove('open');
      document.getElementById('ann-backdrop').classList.remove('visible');
    }}

    document.addEventListener('DOMContentLoaded', () => {{
      const fab = document.getElementById('ann-fab');
      fab.addEventListener('click', () => openSheet('ann-panel-sheet'));

      document.getElementById('ann-panel-close')
        .addEventListener('click', () => closeSheet('ann-panel-sheet'));

      document.getElementById('ann-backdrop').addEventListener('click', () => {{
        closeSheet('ann-panel-sheet');
        closeSheet('ann-action-sheet');
        closeSheet('ann-comment-sheet');
        _pendingSelection = null;
      }});

      loadAnnotations();
    }});
  </script>
```

- [ ] **Step 3: Add FAB, backdrop, panel sheet HTML to PREVIEW_HTML_TEMPLATE body**

Add inside `<body>` after `<div id="toast">`:

```html
  <button id="ann-fab" title="Annotations">💬</button>
  <div id="ann-backdrop"></div>

  <!-- Annotations list panel (bottom sheet) -->
  <div id="ann-panel-sheet" class="ann-sheet">
    <div class="ann-sheet-handle"></div>
    <div class="ann-sheet-header">
      Annotations
      <button class="ann-sheet-close" id="ann-panel-close">✕</button>
    </div>
    <div class="ann-sheet-body" id="ann-panel-body"></div>
  </div>
```

- [ ] **Step 4: Pass ann_file_key in serve_preview**

In `serve_preview`, derive the annotation key from `url_path`:

```python
        ann_file_key = url_path[len(PREFIX):]  # e.g. '/docs/file.md'
```

Add `ann_file_key=html.escape(ann_file_key)` to the `PREVIEW_HTML_TEMPLATE.format(...)` call. The JS block uses `'{ann_file_key}'` (single braces) — the template wraps all other JS in double-braces, so this single-brace value will be substituted by Python's `.format()`.

- [ ] **Step 5: Manual smoke test**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 file_share_serve.py 3458 &
# Open https://neo.taileffc7.ts.net/files/docs/some-file.md
# Expected: FAB (💬) appears bottom-right
# Tap FAB → bottom sheet slides up showing "No annotations yet"
```

- [ ] **Step 6: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
git add file_share_serve.py
git commit -m "feat(file-share): annotation highlights + FAB + bottom-sheet panel"
```

---

### Task 5: Frontend — selection action bar and comment sheet (mobile-first)

**Files:**
- Modify: `scratch/file_share_serve.py` — add action bar + comment sheet HTML/CSS/JS

The **action bar** is a fixed bottom strip (full width, 64px tall) with three large buttons. It replaces the floating popup: no positioning math, no finger-occlusion problem. On desktop it looks like a toolbar; on mobile it sits above the virtual keyboard. The **comment sheet** is a second bottom sheet with a textarea — when focused on mobile the sheet slides above the keyboard automatically via `env(safe-area-inset-bottom)`.

- [ ] **Step 1: Add action bar + comment sheet CSS**

Add before `</style>`:

```css
    /* Selection action bar — slides up from bottom when text is selected */
    #ann-action-sheet {{
      position: fixed;
      bottom: 0; left: 0; right: 0;
      background: #161b22;
      border-top: 1px solid #30363d;
      z-index: 200;
      transform: translateY(100%);
      transition: transform 0.2s cubic-bezier(0.4,0,0.2,1);
      display: flex;
      align-items: stretch;
      height: 64px;
      padding-bottom: env(safe-area-inset-bottom);
    }}
    #ann-action-sheet.open {{ transform: translateY(0); }}

    .ann-action-btn {{
      flex: 1;
      background: none;
      border: none;
      border-right: 1px solid #21262d;
      font-size: 22px;
      cursor: pointer;
      color: #c9d1d9;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 2px;
      transition: background 0.1s;
      -webkit-tap-highlight-color: transparent;
      min-height: 64px;
    }}
    .ann-action-btn:last-child {{ border-right: none; }}
    .ann-action-btn:hover, .ann-action-btn:active {{ background: #21262d; }}
    .ann-action-btn span {{ font-size: 10px; color: #8b949e; letter-spacing: 0.3px; }}

    /* Comment entry sheet */
    #ann-comment-sheet {{
      position: fixed;
      bottom: 0; left: 0; right: 0;
      background: #161b22;
      border-top: 1px solid #30363d;
      border-radius: 16px 16px 0 0;
      z-index: 210;
      transform: translateY(100%);
      transition: transform 0.25s cubic-bezier(0.4,0,0.2,1);
      padding: 16px 16px calc(16px + env(safe-area-inset-bottom));
    }}
    #ann-comment-sheet.open {{ transform: translateY(0); }}
    #ann-comment-label {{
      font-size: 13px;
      color: #8b949e;
      margin-bottom: 8px;
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    #ann-comment-input {{
      width: 100%;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 8px;
      color: #c9d1d9;
      font-size: 15px;
      padding: 10px 12px;
      resize: none;
      height: 80px;
      font-family: inherit;
      box-sizing: border-box;
    }}
    #ann-comment-input:focus {{ outline: none; border-color: #58a6ff; }}
    #ann-comment-actions {{
      margin-top: 10px;
      display: flex;
      gap: 10px;
      justify-content: flex-end;
    }}
```

- [ ] **Step 2: Add action bar + comment sheet HTML**

Add after the panel sheet `</div>`:

```html
  <!-- Action bar: appears when text is selected -->
  <div id="ann-action-sheet">
    <button class="ann-action-btn" id="ann-up">👍<span>Like</span></button>
    <button class="ann-action-btn" id="ann-down">👎<span>Dislike</span></button>
    <button class="ann-action-btn" id="ann-cmt">💬<span>Comment</span></button>
  </div>

  <!-- Comment entry sheet -->
  <div id="ann-comment-sheet">
    <span id="ann-comment-label">Add comment</span>
    <textarea id="ann-comment-input" placeholder="What do you want to say?" rows="3"></textarea>
    <div id="ann-comment-actions">
      <button class="btn" id="ann-comment-cancel">Cancel</button>
      <button class="btn btn-primary" id="ann-comment-save">Save</button>
    </div>
  </div>
```

- [ ] **Step 3: Add selection capture + action bar JS**

Add inside the annotation `<script>` block, before the `DOMContentLoaded` listener:

```javascript
    // ── Selection capture ────────────────────────────────────────────────────

    let _pendingSelection = null;

    function getContentCharOffset(node, localOffset) {{
      const textNodes = getPreviewTextNodes();
      let total = 0;
      for (const tn of textNodes) {{
        if (tn === node) return total + localOffset;
        total += tn.textContent.length;
      }}
      return total + localOffset;
    }}

    function captureSelection() {{
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.toString().trim() === '') return null;
      const range = sel.getRangeAt(0);
      const root = document.querySelector('.file-box pre code, .file-box .markdown-body, .file-box pre');
      if (!root || !root.contains(range.commonAncestorContainer)) return null;
      return {{
        selectedText: sel.toString(),
        offsetStart: getContentCharOffset(range.startContainer, range.startOffset),
        offsetEnd:   getContentCharOffset(range.endContainer,   range.endOffset),
      }};
    }}

    // selectionchange fires on both desktop and mobile (after touch selection handles appear)
    let _selectionTimer = null;
    document.addEventListener('selectionchange', () => {{
      clearTimeout(_selectionTimer);
      _selectionTimer = setTimeout(() => {{
        const capture = captureSelection();
        if (capture) {{
          _pendingSelection = capture;
          openSheet('ann-action-sheet');
        }} else {{
          // Only close if action sheet is open and nothing is in progress
          const sheet = document.getElementById('ann-action-sheet');
          if (sheet.classList.contains('open') && !_pendingSelection) {{
            closeSheet('ann-action-sheet');
          }}
        }}
      }}, 150);  // 150ms debounce — lets mobile selection handles settle
    }});

    async function saveAnnotation(type, comment = '') {{
      if (!_pendingSelection) return;
      const {{ selectedText, offsetStart, offsetEnd }} = _pendingSelection;
      _pendingSelection = null;
      window.getSelection()?.removeAllRanges();
      closeSheet('ann-action-sheet');
      const resp = await fetch('/files-api/annotations', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ file: ANN_FILE, selected_text: selectedText,
          offset_start: offsetStart, offset_end: offsetEnd,
          type, comment, author: 'hal' }}),
      }});
      if (resp.ok) loadAnnotations();
    }}
```

- [ ] **Step 4: Wire up buttons in DOMContentLoaded**

Add to the `DOMContentLoaded` listener, before `loadAnnotations()`:

```javascript
      document.getElementById('ann-up').addEventListener('click', () => saveAnnotation('upvote'));
      document.getElementById('ann-down').addEventListener('click', () => saveAnnotation('downvote'));

      document.getElementById('ann-cmt').addEventListener('click', () => {{
        if (!_pendingSelection) return;
        const preview = _pendingSelection.selectedText.slice(0, 60);
        document.getElementById('ann-comment-label').textContent =
          `"${{preview}}${{_pendingSelection.selectedText.length > 60 ? '…' : ''}}"`;
        document.getElementById('ann-comment-input').value = '';
        closeSheet('ann-action-sheet');
        openSheet('ann-comment-sheet');
        // Delay focus so sheet animation completes before keyboard pops up
        setTimeout(() => document.getElementById('ann-comment-input').focus(), 260);
      }});

      document.getElementById('ann-comment-cancel').addEventListener('click', () => {{
        closeSheet('ann-comment-sheet');
        _pendingSelection = null;
      }});

      document.getElementById('ann-comment-save').addEventListener('click', () => {{
        const comment = document.getElementById('ann-comment-input').value.trim();
        if (!comment) return;
        closeSheet('ann-comment-sheet');
        saveAnnotation('comment', comment);
      }});

      document.addEventListener('keydown', e => {{
        if (e.key === 'Escape') {{
          closeSheet('ann-action-sheet');
          closeSheet('ann-comment-sheet');
          _pendingSelection = null;
        }}
      }});
```

- [ ] **Step 5: Manual smoke test — mobile device**

```bash
kill $(lsof -ti :3458) 2>/dev/null
python3 /home/halr9000/.openclaw/workspace/scratch/file_share_serve.py 3458 &
```

Open https://neo.taileffc7.ts.net/files/ on a mobile browser:
1. Long-press text to start selection → adjust handles → action bar slides up from bottom with 👍 👎 💬
2. Tap 👍 → bar dismisses, FAB shows `👍1`
3. Tap text again to select → tap 💬 → comment sheet slides up → type comment → Save
4. Tap FAB → panel sheet shows all annotations with text previews
5. Tap ✕ on an annotation → it disappears

- [ ] **Step 6: Commit**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
git add file_share_serve.py
git commit -m "feat(file-share): mobile-first annotation action bar + comment sheet"
```

---

### Task 6: Copy file_share_serve.py back over file-share-serve.py and update SKILL.md

The canonical served file is still `file-share-serve.py` (with hyphen) per the systemd service. Keep both in sync.

**Files:**
- Modify: `scratch/file-share-serve.py` ← sync from `file_share_serve.py`
- Modify: `skills/file-share/SKILL.md` — add annotation section

- [ ] **Step 1: Sync files**

```bash
cp /home/halr9000/.openclaw/workspace/scratch/file_share_serve.py \
   /home/halr9000/.openclaw/workspace/scratch/file-share-serve.py
systemctl --user restart file-share.service
systemctl --user status file-share.service
```

Expected: Active (running).

- [ ] **Step 2: Add annotation section to SKILL.md**

Add this section after `## Security notes` in `skills/file-share/SKILL.md`:

```markdown
## Annotations

Annotations are stored at `/home/halr9000/shared/.annotations.json`. Each entry:

```json
{
  "id": "uuid",
  "file": "/docs/file.md",
  "selected_text": "the annotated text",
  "offset_start": 0,
  "offset_end": 11,
  "type": "upvote" | "downvote" | "comment",
  "comment": "optional comment body",
  "author": "hal",
  "created_at": "2026-06-10T19:00:00Z"
}
```

### Jeeves: read all annotations for a file

```python
import json
from pathlib import Path
anns = json.loads(Path('/home/halr9000/shared/.annotations.json').read_text())
for a in anns:
    if a['file'] == '/docs/target-file.md':
        print(f"[{a['type']}] «{a['selected_text'][:60]}» {a.get('comment','')}")
```

Or via the API (useful from a subagent without filesystem access):

```bash
curl -s "https://neo.taileffc7.ts.net/files-api/annotations?file=/docs/target-file.md" | jq .
```

### Jeeves: summarize annotations on a shared doc

When Hal asks you to review a shared document with annotations:
1. Read the file: `Path('/home/halr9000/shared/PATH').read_text()`
2. Read its annotations: filter `.annotations.json` by `file == '/PATH'`
3. Cross-reference `selected_text` + `offset_start/end` with the file content
4. Surface upvotes (sections Hal likes), downvotes (sections to revise), comments (specific feedback)
```

- [ ] **Step 3: Run full test suite one final time**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
python3 -m pytest tests/test_annotations.py -v
```

Expected: All tests pass.

- [ ] **Step 4: Final commit**

```bash
cd /home/halr9000/.openclaw/workspace/scratch
git add file-share-serve.py
cd /home/halr9000/.openclaw/workspace
git add skills/file-share/SKILL.md 2>/dev/null || true
git commit -m "feat(file-share): wire annotations into live service + update SKILL.md"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Select text → popup — Task 5
- [x] Upvote / downvote / comment — Tasks 3, 5
- [x] Persist on server — Tasks 1, 2
- [x] Jeeves can read — Task 6 (SKILL.md, JSON format, API endpoint)
- [x] Collaboration on documentation — SKILL.md "Jeeves: summarize annotations" section

**No placeholders:** Reviewed — all steps contain complete code.

**Type consistency:**
- `AnnotationStore.add()` signature matches usage in `do_POST` (Task 3) ✓
- `ANN_FILE` JS constant wired via `ann_file_key` format param (Task 4) ✓
- `loadAnnotations()` defined before `DOMContentLoaded` wires it up (Task 4, 5) ✓
- `getPreviewTextNodes()` used in both `applyHighlights` (Task 4) and `getContentCharOffset` (Task 5) ✓
- `_store` global instance used by `do_POST`, `do_GET`, `do_DELETE` ✓
