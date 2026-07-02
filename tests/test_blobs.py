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
