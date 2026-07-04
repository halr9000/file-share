"""Tests for file-share annotation feature."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent))
import file_share_serve as fss


class TestAnnotationStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmpdir.name) / '.annotations.json'
        self.store = fss.AnnotationStore(self.store_path)

    def tearDown(self):
        if hasattr(self, 'tmpdir'):
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


class TestAnnotationAPI(unittest.TestCase):
    """Integration tests: spins up a real server, hits it with http.client."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        fss.SHARED_DIR = Path(cls.tmpdir.name)
        fss.ANNOTATIONS_FILE = fss.SHARED_DIR / '.annotations.json'
        fss._store = fss.AnnotationStore(fss.ANNOTATIONS_FILE)
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

    def test_post_stores_html_special_characters_verbatim(self):
        # The server does not (and should not) sanitize annotation content --
        # the preview page's client-side rendering is responsible for safely
        # displaying it (via textContent, not innerHTML string interpolation).
        # This test documents that contract: whatever the client sends is
        # what comes back, unescaped.
        payload = '<script>alert(1)</script>'
        status, body = self._post({
            'file': '/xss-test.md', 'selected_text': payload,
            'offset_start': 0, 'offset_end': 1,
            'type': 'comment', 'comment': payload, 'author': payload,
        })
        self.assertEqual(status, 201)
        self.assertEqual(body['selected_text'], payload)
        self.assertEqual(body['comment'], payload)
        self.assertEqual(body['author'], payload)

    def test_post_rejects_body_exceeding_max_upload_bytes(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        oversized = fss.MAX_UPLOAD_BYTES + 1
        conn.request('POST', '/files-api/annotations', body=b'x',
                     headers={'Content-Type': 'application/json',
                              'Content-Length': str(oversized)})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        resp.read()

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

    def test_post_creates_unanchored_annotation_with_null_offsets(self):
        status, body = self._post({
            'file': '/picture.jpg', 'type': 'comment',
            'comment': 'nice photo', 'author': 'hal',
        })
        self.assertEqual(status, 201)
        self.assertIsNone(body['offset_start'])
        self.assertIsNone(body['offset_end'])
        self.assertEqual(body['selected_text'], '')

    def test_post_creates_unanchored_annotation_with_explicit_null_offsets(self):
        status, body = self._post({
            'file': '/picture.jpg', 'type': 'comment', 'comment': 'nice photo',
            'offset_start': None, 'offset_end': None, 'author': 'hal',
        })
        self.assertEqual(status, 201)
        self.assertIsNone(body['offset_start'])
        self.assertIsNone(body['offset_end'])

    def test_post_rejects_only_one_offset_set(self):
        status, body = self._post({
            'file': '/picture.jpg', 'type': 'comment', 'comment': 'x',
            'offset_start': 0, 'author': 'hal',
        })
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_get_returns_unanchored_annotation_alongside_anchored(self):
        self._post({'file': '/mixed.md', 'type': 'comment', 'comment': 'general note', 'author': 'hal'})
        self._post({'file': '/mixed.md', 'selected_text': 'x', 'offset_start': 0,
                     'offset_end': 1, 'type': 'upvote', 'comment': '', 'author': 'hal'})
        status, body = self._get('/mixed.md')
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 2)
        self.assertTrue(any(a['offset_start'] is None for a in body))
        self.assertTrue(any(a['offset_start'] is not None for a in body))


if __name__ == '__main__':
    unittest.main()
