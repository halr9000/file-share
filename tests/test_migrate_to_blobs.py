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


class TestResolveSharedDir(unittest.TestCase):

    def test_env_var_wins(self):
        result = migrate_mod.resolve_shared_dir(
            ['migrate_to_blobs.py'], {'FILE_SHARE_DIR': '/from/env'})
        self.assertEqual(result, Path('/from/env'))

    def test_falls_back_to_argv(self):
        result = migrate_mod.resolve_shared_dir(
            ['migrate_to_blobs.py', '/from/argv'], {})
        self.assertEqual(result, Path('/from/argv'))

    def test_env_var_takes_precedence_over_argv(self):
        result = migrate_mod.resolve_shared_dir(
            ['migrate_to_blobs.py', '/from/argv'], {'FILE_SHARE_DIR': '/from/env'})
        self.assertEqual(result, Path('/from/env'))

    def test_raises_when_neither_set(self):
        with self.assertRaises(SystemExit):
            migrate_mod.resolve_shared_dir(['migrate_to_blobs.py'], {})


if __name__ == '__main__':
    unittest.main()
