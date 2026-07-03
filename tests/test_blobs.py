"""Tests for file-share blob CRUD helpers and API."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

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

    def _get(self, path: str):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET', path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

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

    def test_post_retries_on_id_collision_with_different_filename(self):
        _, first = self._post_blob('existing.md', b'first')
        colliding_id = first['id']
        with patch.object(fss, 'generate_blob_id', side_effect=[colliding_id, 'ffffffff']):
            status, second = self._post_blob('other.md', b'second')
        self.assertEqual(status, 201)
        self.assertEqual(second['id'], 'ffffffff')
        self.assertNotEqual(second['id'], colliding_id)

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
        # The href must still route to the id-prefixed file on disk; only the
        # visible anchor text should hide the id.
        self.assertIn(f'>visible-name.md</a>', html_body)
        self.assertNotIn(f'>{blob["id"]}-visible-name.md</a>', html_body)

    def test_preview_page_shows_clean_filename_and_ids_for_annotations(self):
        _, blob = self._post_blob('preview-me.md', b'# Title')
        status, html_body = self._get_html(f'/files/{blob["id"]}-preview-me.md')
        self.assertEqual(status, 200)
        self.assertIn('preview-me.md', html_body)
        self.assertIn(f"ANN_FILE = '{blob['id']}'", html_body)

    def test_preview_page_breadcrumb_has_no_duplicate_files_segment(self):
        _, blob = self._post_blob('crumb-test.md', b'# hi')
        status, html_body = self._get_html(f'/files/{blob["id"]}-crumb-test.md')
        self.assertEqual(status, 200)
        self.assertNotIn('/files/files/', html_body)
        self.assertNotIn('>files/</a>', html_body)


if __name__ == '__main__':
    unittest.main()
