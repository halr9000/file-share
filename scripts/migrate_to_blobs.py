#!/usr/bin/env python3
"""One-time migration: rename flat shared/ files to <id>-<filename> blobs
and remap .annotations.json file keys from path strings to blob ids.

Safe to re-run: files already in <id>-<filename> form and annotations
already keyed by a bare 8-hex id are left untouched.

Usage: FILE_SHARE_DIR=/path/to/shared python3 scripts/migrate_to_blobs.py
   or: python3 scripts/migrate_to_blobs.py /path/to/shared
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path

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

    backup = annotations_file.with_suffix('.json.bak')

    renamed = {}
    for p in sorted(shared_dir.iterdir()):
        if not p.is_file() or p.name in (annotations_file.name, backup.name):
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


def resolve_shared_dir(argv, environ) -> Path:
    """Resolve the shared dir from FILE_SHARE_DIR env var or argv[1].

    Raises SystemExit with a helpful message if neither is set.
    """
    value = environ.get('FILE_SHARE_DIR') or (argv[1] if len(argv) > 1 else None)
    if not value:
        raise SystemExit(
            'Set FILE_SHARE_DIR env var or pass the shared directory as an argument.'
        )
    return Path(value)


if __name__ == '__main__':
    shared_dir = resolve_shared_dir(sys.argv, os.environ)
    annotations_file = shared_dir / '.annotations.json'
    migrate(shared_dir, annotations_file)
