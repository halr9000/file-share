#!/usr/bin/env python3
"""
file-share-serve.py
Gist-style file preview server for a flat blob-store directory.

Serves files under the /files/ path prefix. Configure the served
directory and port via the FILE_SHARE_DIR and PORT env vars (see README).


- Default: serves a GitHub Gist-style HTML preview page
- ?raw=1: serves the file directly with Content-Disposition: attachment
- Directories: HTML listing with links to preview pages

Usage:
  python3 file-share-serve.py [port]

Default port: 3458
"""
# ── Table of Contents ───────────────────────────────────────────────────────
# 1.  Imports & constants (~line 19)
# 2.  MIME/extension maps (~line 36)
# 3.  AnnotationStore — thread-safe JSON-backed annotation storage (~line 85)
# 4.  Utility functions (~line 140)
# 5.  PREVIEW_HTML_TEMPLATE — full page HTML/CSS/JS (~line 196)
#     5a. CSS: layout, topbar, file-box, markdown, media, annotation UI
#     5b. HTML: topbar, content, FAB, sheets (action/comment/detail/panel/TOC)
#     5c. JS: cookie helpers, wrap, highlight, annotation system, TOC, back-to-top
# 6.  DIR_HTML_TEMPLATE — directory listing (~line 975)
# 7.  GistHandler — HTTP request handler (~line 977)
#     7a. do_HEAD, serve_raw, serve_directory, serve_preview
#     7b. do_GET (file preview + /files-api/ annotations GET)
#     7c. do_POST, do_DELETE, do_PATCH (annotation API)
# 8.  Entry point (~line 1330)
# ─────────────────────────────────────────────────────────────────────────────

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

_argv1 = sys.argv[1] if len(sys.argv) > 1 else None
PORT = int(_argv1) if (_argv1 is not None and _argv1.isdigit()) else int(os.environ.get('PORT', '3458'))
SHARED_DIR = Path(os.environ.get('FILE_SHARE_DIR', './shared'))
PREFIX = '/files'
MAX_UPLOAD_BYTES = int(os.environ.get('FILE_SHARE_MAX_UPLOAD_BYTES', 100 * 1024 * 1024))  # 100 MB default

# MIME types for syntax highlighting language detection
HIGHLIGHT_LANG_MAP = {
    '.xml': 'xml',
    '.json': 'json',
    '.py': 'python',
    '.sh': 'bash',
    '.bash': 'bash',
    '.zsh': 'bash',
    '.js': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.jsx': 'javascript',
    '.yaml': 'yaml',
    '.yml': 'yaml',
    '.toml': 'toml',
    '.ini': 'ini',
    '.cfg': 'ini',
    '.conf': 'ini',
    '.css': 'css',
    '.html': 'html',
    '.htm': 'html',
    '.sql': 'sql',
    '.rs': 'rust',
    '.go': 'go',
    '.c': 'c',
    '.cpp': 'cpp',
    '.h': 'cpp',
    '.java': 'java',
    '.rb': 'ruby',
    '.lua': 'lua',
    '.md': 'markdown',
    '.txt': 'plaintext',
    '.log': 'plaintext',
    '.csv': 'plaintext',
    '.diff': 'diff',
    '.patch': 'diff',
    '.dockerfile': 'dockerfile',
}

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico'}
AUDIO_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus'}
VIDEO_EXTS = {'.mp4', '.webm', '.mkv', '.avi', '.mov'}

TEXT_MIMES = {'text/', 'application/json', 'application/xml', 'application/javascript',
              'application/x-sh', 'application/toml', 'application/yaml'}

ANNOTATIONS_FILE = SHARED_DIR / '.annotations.json'


# ── 3. AnnotationStore ─────────────────────────────────────
class AnnotationStore:
    """Thread-safe, JSON-file-backed store for text annotations.

    Annotations are dicts with keys: id, file, selected_text, offset_start,
    offset_end, type (upvote|downvote|comment), comment, author, created_at.
    offset_start/offset_end are None for an unanchored (whole-file) comment.
    All mutations hold ``_lock`` and flush to disk immediately.
    """

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

    def add(self, file: str, selected_text: str, offset_start: int | None, offset_end: int | None,
            ann_type: str, comment: str, author: str) -> dict:
        """Append a new annotation and persist. Returns the new annotation dict.

        offset_start/offset_end may be None for an "unanchored" annotation --
        a general comment on the file as a whole, not tied to a text range
        (e.g. a comment on an image).
        """
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
        """Return all annotations for the given file path key."""
        with self._lock:
            return [a for a in self._data if a['file'] == file]

    def get_all(self) -> list[dict]:
        """Return all annotations across all files."""
        with self._lock:
            return list(self._data)

    def delete(self, ann_id: str) -> bool:
        """Delete annotation by id. Returns True if found and deleted."""
        with self._lock:
            original_len = len(self._data)
            self._data = [a for a in self._data if a['id'] != ann_id]
            if len(self._data) < original_len:
                self._save()
                return True
            return False

    def delete_by_file(self, file_key: str) -> int:
        """Delete every annotation for the given file key. Returns count deleted."""
        with self._lock:
            original_len = len(self._data)
            self._data = [a for a in self._data if a['file'] != file_key]
            deleted = original_len - len(self._data)
            if deleted:
                self._save()
            return deleted

    def update(self, ann_id: str, comment: str | None = None, ann_type: str | None = None) -> dict | None:
        """Update comment and/or type. Returns the updated dict, or None if not found."""
        with self._lock:
            for ann in self._data:
                if ann['id'] == ann_id:
                    if comment is not None:
                        ann['comment'] = comment
                    if ann_type is not None:
                        ann['type'] = ann_type
                    self._save()
                    return dict(ann)
            return None


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


# ── 4. Utility functions ───────────────────────────────────
def format_size(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            return f'{size:.1f} {unit}' if unit != 'B' else f'{size} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def is_text_mime(mime):
    if not mime:
        return False
    for t in TEXT_MIMES:
        if mime.startswith(t):
            return True
    return False


def get_preview_type(filepath):
    """Return one of: markdown, code, image, audio, video, text, binary"""
    suffix = filepath.suffix.lower()
    # Check compound suffixes like .tsk.xml
    name_lower = filepath.name.lower()
    for compound in ('.tsk.xml', '.prj.xml', '.prf.xml'):
        if name_lower.endswith(compound):
            suffix = '.xml'
            break

    if suffix == '.md':
        return 'markdown'
    if suffix in IMAGE_EXTS:
        return 'image'
    if suffix in AUDIO_EXTS:
        return 'audio'
    if suffix in VIDEO_EXTS:
        return 'video'
    if suffix in HIGHLIGHT_LANG_MAP:
        lang = HIGHLIGHT_LANG_MAP[suffix]
        return ('text' if lang == 'plaintext' else 'code')

    # Fall back to MIME type
    mime, _ = mimetypes.guess_type(str(filepath))
    if is_text_mime(mime):
        return 'text'
    return 'binary'


def get_highlight_lang(filepath):
    suffix = filepath.suffix.lower()
    name_lower = filepath.name.lower()
    for compound in ('.tsk.xml', '.prj.xml', '.prf.xml'):
        if name_lower.endswith(compound):
            return 'xml'
    return HIGHLIGHT_LANG_MAP.get(suffix, 'plaintext')


# ── 5. HTML templates ──────────────────────────────────────
PREVIEW_HTML_TEMPLATE = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet"
    href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
  <link rel="stylesheet"
    href="https://fonts.googleapis.com/icon?family=Material+Icons">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #c9d1d9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
      font-size: 14px;
    }}
    a {{ color: #58a6ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    /* top bar */
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 20px;
      background: #161b22;
      border-bottom: 1px solid #30363d;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .topbar .meta {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .filename {{
      font-weight: 600;
      font-size: 15px;
      color: #e6edf3;
    }}
    .badge {{
      background: #21262d;
      border: 1px solid #30363d;
      border-radius: 12px;
      padding: 2px 8px;
      font-size: 12px;
      color: #8b949e;
    }}
    .actions {{
      display: flex;
      gap: 8px;
    }}
    .btn {{
      padding: 5px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      border: 1px solid #30363d;
      background: #21262d;
      color: #c9d1d9;
      text-decoration: none;
      display: inline-block;
      transition: background 0.15s;
    }}
    .btn:hover {{
      background: #30363d;
      text-decoration: none;
      color: #e6edf3;
    }}
    .btn-primary {{
      background: #238636;
      border-color: #2ea043;
      color: #fff;
    }}
    .btn-primary:hover {{ background: #2ea043; color: #fff; }}

    /* content */
    .content {{
      max-width: 1200px;
      margin: 24px auto;
      padding: 0 20px;
    }}
    .file-box {{
      border: 1px solid #30363d;
      border-radius: 6px;
      overflow: hidden;
    }}
    .file-box pre {{
      margin: 0;
      padding: 16px;
      overflow-x: auto;
      background: #161b22;
      line-height: 1.5;
    }}
    .file-box pre.wrap {{
      white-space: pre-wrap;
      overflow-x: hidden;
      word-break: break-all;
    }}
    .file-box pre code {{
      background: none !important;
      padding: 0 !important;
      font-size: 13px;
    }}

    /* markdown */
    .markdown-body {{
      padding: 24px;
      background: #161b22;
      line-height: 1.6;
    }}
    .markdown-body h1, .markdown-body h2, .markdown-body h3 {{
      border-bottom: 1px solid #30363d;
      padding-bottom: 6px;
      color: #e6edf3;
    }}
    .markdown-body code {{
      background: #1f2428;
      padding: 2px 5px;
      border-radius: 3px;
      font-size: 85%;
    }}
    .markdown-body pre {{ background: #1f2428; padding: 16px; border-radius: 6px; overflow-x: auto; }}
    .markdown-body pre code {{ background: none; padding: 0; }}
    .markdown-body blockquote {{
      border-left: 4px solid #30363d;
      margin: 0; padding-left: 16px; color: #8b949e;
    }}

    /* media */
    .media-box {{
      padding: 24px;
      background: #161b22;
      display: flex;
      justify-content: center;
    }}
    .media-box img {{ max-width: 100%; max-height: 80vh; border-radius: 4px; }}
    .media-box audio, .media-box video {{ width: 100%; max-width: 800px; }}

    /* no-preview */
    .no-preview {{
      padding: 40px;
      background: #161b22;
      text-align: center;
      color: #8b949e;
    }}
    .no-preview .icon {{ font-size: 48px; margin-bottom: 12px; }}

    /* breadcrumb */
    .breadcrumb {{
      padding: 0 0 12px 0;
      color: #8b949e;
      font-size: 13px;
    }}
    .breadcrumb a {{ color: #58a6ff; }}

    /* copy toast */
    #toast {{
      position: fixed;
      top: 16px;
      left: 50%;
      transform: translateX(-50%);
      background: #238636;
      color: #fff;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      opacity: 0;
      transition: opacity 0.3s;
      pointer-events: none;
      z-index: 1000;
    }}

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

    /* Back-to-top button — sits above the FAB */
    #back-to-top {{
      position: fixed;
      bottom: 84px;
      right: 20px;
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: #21262d;
      border: 1px solid #30363d;
      color: #8b949e;
      font-size: 18px;
      cursor: pointer;
      z-index: 99;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      -webkit-tap-highlight-color: transparent;
    }}
    #back-to-top.visible {{ opacity: 1; pointer-events: auto; }}
    #back-to-top:hover {{ background: #30363d; color: #e6edf3; }}

    /* TOC FAB — top-left, markdown only */
    #toc-fab {{
      display: none;
      position: fixed;
      top: 64px;
      left: 12px;
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: rgba(33,38,45,0.85);
      border: 1px solid #30363d;
      color: #8b949e;
      font-size: 16px;
      cursor: pointer;
      z-index: 150;
      align-items: center;
      justify-content: center;
      backdrop-filter: blur(4px);
      -webkit-tap-highlight-color: transparent;
      transition: background 0.15s, color 0.15s;
    }}
    #toc-fab.open {{ background: rgba(31,111,235,0.3); border-color: #58a6ff; color: #58a6ff; }}
    #toc-fab:hover {{ background: rgba(48,54,61,0.9); color: #e6edf3; }}

    #toc-flyout {{
      display: none;
      position: fixed;
      top: 64px;
      left: 56px;
      width: min(260px, calc(100vw - 72px));
      max-height: calc(100vh - 80px);
      overflow-y: auto;
      background: rgba(22,27,34,0.96);
      border: 1px solid #30363d;
      border-radius: 8px;
      z-index: 149;
      padding: 6px 0;
      backdrop-filter: blur(6px);
      box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    }}
    #toc-flyout.open {{ display: block; }}
    .toc-item {{
      display: block;
      padding: 5px 14px;
      font-size: 13px;
      color: #c9d1d9;
      text-decoration: none;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: background 0.1s;
    }}
    .toc-item:hover {{ background: #21262d; color: #e6edf3; text-decoration: none; }}
    .toc-level-1 {{ font-weight: 600; color: #e6edf3; }}
    .toc-level-2 {{ padding-left: 22px; }}
    .toc-level-3 {{ padding-left: 34px; font-size: 12px; color: #8b949e; }}
    .toc-level-4, .toc-level-5, .toc-level-6 {{ padding-left: 46px; font-size: 11px; color: #6e7681; }}

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
    #ann-panel-sheet {{
      max-height: none;
      height: var(--panel-h, 60vh);
    }}
    .ann-sheet-handle {{
      width: 36px; height: 4px;
      background: #30363d;
      border-radius: 2px;
      margin: 10px auto 0;
      flex-shrink: 0;
    }}
    #ann-panel-sheet .ann-sheet-handle {{
      cursor: ns-resize;
      padding: 8px 0;
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
      align-items: center;
      gap: 10px;
    }}

    /* Annotation detail sheet */
    #ann-detail-sheet {{
      position: fixed;
      bottom: 0; left: 0; right: 0;
      background: #161b22;
      border-top: 1px solid #30363d;
      border-radius: 16px 16px 0 0;
      z-index: 210;
      transform: translateY(100%);
      transition: transform 0.25s cubic-bezier(0.4,0,0.2,1);
      padding: 0 16px calc(16px + env(safe-area-inset-bottom));
    }}
    #ann-detail-sheet.open {{ transform: translateY(0); }}
    #ann-detail-snippet {{
      font-size: 13px; color: #8b949e; font-style: italic;
      border-left: 3px solid #30363d;
      padding: 6px 10px; margin: 12px 0 10px;
      overflow: hidden; text-overflow: ellipsis;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    }}
    .ann-type-btns {{
      display: flex; gap: 8px; margin-bottom: 12px;
    }}
    .ann-type-btn {{
      flex: 1; padding: 8px 0; border-radius: 8px;
      border: 1px solid #30363d; background: #21262d;
      color: #c9d1d9; font-size: 18px; cursor: pointer;
      display: flex; flex-direction: column; align-items: center; gap: 3px;
      transition: background 0.12s, border-color 0.12s;
      -webkit-tap-highlight-color: transparent;
    }}
    .ann-type-btn span {{ font-size: 10px; color: #8b949e; }}
    .ann-type-btn.active-upvote   {{ background: rgba(46,160,67,0.25); border-color: #3fb950; color: #3fb950; }}
    .ann-type-btn.active-downvote {{ background: rgba(248,81,73,0.25); border-color: #f85149; color: #f85149; }}
    .ann-type-btn.active-comment  {{ background: rgba(88,166,255,0.25); border-color: #58a6ff; color: #58a6ff; }}
    .ann-type-btn.active-upvote span, .ann-type-btn.active-downvote span, .ann-type-btn.active-comment span {{ color: inherit; }}
    #ann-detail-comment {{
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
    #ann-detail-comment:focus {{ outline: none; border-color: #58a6ff; }}
    #ann-detail-footer {{
      display: flex; align-items: center; justify-content: space-between;
      margin-top: 10px;
    }}
    #ann-detail-saved {{
      font-size: 12px; color: #3fb950; opacity: 0;
      transition: opacity 0.3s;
    }}
    #ann-detail-saved.visible {{ opacity: 1; }}
    #ann-detail-delete {{
      background: none; border: 1px solid #30363d; color: #484f58;
      font-size: 13px; cursor: pointer; padding: 6px 10px;
      border-radius: 6px;
    }}
    #ann-detail-delete:hover {{ color: #f85149; border-color: #f85149; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="meta">
      <span class="filename">{filename}</span>
      <span class="badge">{size}</span>
      <span class="badge">{mime}</span>
    </div>
    <div class="actions">
      {copy_btn}
      {wrap_btn}
      <a class="btn" href="{raw_url}"><span class="material-icons" style="font-size:16px;vertical-align:-3px;margin-right:4px">code</span>Raw</a>
      <a class="btn" href="{raw_url}" download="{filename}"><span class="material-icons" style="font-size:16px;vertical-align:-3px;margin-right:4px">download</span>Download</a>
    </div>
  </div>

  <div class="content">
    <div class="breadcrumb">{breadcrumb}</div>
    <div class="file-box">
      {preview_content}
    </div>
  </div>

  <div id="toast">Copied to clipboard!</div>

  <button id="ann-fab" title="Annotations">💬</button>
  <button id="toc-fab" title="Table of contents" aria-label="Table of contents">☰</button>
  <nav id="toc-flyout" aria-label="Table of contents"></nav>
  <a id="top"></a>
  <button id="back-to-top" title="Back to top" aria-label="Back to top">↑</button>
  <div id="ann-backdrop"></div>

  <!-- Annotations list panel (bottom sheet) -->
  <div id="ann-panel-sheet" class="ann-sheet">
    <div class="ann-sheet-handle"></div>
    <div class="ann-sheet-header">
      Annotations
      <button class="btn" id="ann-panel-add-general" title="Add a comment not tied to a specific selection — e.g. on an image">+ Comment</button>
      <button class="ann-sheet-close" id="ann-panel-close">✕</button>
    </div>
    <div class="ann-sheet-body" id="ann-panel-body"></div>
  </div>

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
    </div>
  </div>

  <!-- Annotation detail sheet (view/edit existing) -->
  <div id="ann-detail-sheet">
    <div class="ann-sheet-handle"></div>
    <div class="ann-sheet-header">
      Annotation
      <button class="ann-sheet-close" id="ann-detail-close">✕</button>
    </div>
    <div id="ann-detail-snippet"></div>
    <div class="ann-type-btns">
      <button class="ann-type-btn" data-type="upvote">👍<span>Like</span></button>
      <button class="ann-type-btn" data-type="downvote">👎<span>Dislike</span></button>
      <button class="ann-type-btn" data-type="comment">💬<span>Note</span></button>
    </div>
    <textarea id="ann-detail-comment" placeholder="Add a comment…"></textarea>
    <div id="ann-detail-footer">
      <span id="ann-detail-saved">✓ Saved</span>
      <button id="ann-detail-delete">Delete annotation</button>
    </div>
  </div>

  <script>
    // ── Cookie helpers ──────────────────────────────────────────────────────
    function setCookie(name, val, days) {{
      const d = new Date(); d.setTime(d.getTime() + days*86400000);
      document.cookie = name+'='+val+';expires='+d.toUTCString()+';path=/';
    }}
    function getCookie(name) {{
      const m = document.cookie.match('(^|;)\\s*'+name+'=([^;]+)');
      return m ? m[2] : null;
    }}

    // Word wrap toggle
    function toggleWrap() {{
      const pre = document.querySelector('.file-box pre');
      if (!pre) return;
      const wrapped = pre.classList.toggle('wrap');
      const btn = document.getElementById('wrapBtn');
      if (btn) btn.style.background = wrapped ? '#1f6feb' : '';
      setCookie('fileshare_wrap', wrapped ? '1' : '0', 365);
    }}

    // Restore wrap preference on load
    (function() {{
      if (getCookie('fileshare_wrap') === '1') {{
        const pre = document.querySelector('.file-box pre');
        if (pre) pre.classList.add('wrap');
        const btn = document.getElementById('wrapBtn');
        if (btn) btn.style.background = '#1f6feb';
      }}
    }})();

    // Syntax highlight all code blocks
    document.querySelectorAll('pre code').forEach(el => {{
      hljs.highlightElement(el);
    }});

    // Copy button
    function copyContent() {{
      const code = document.querySelector('pre code, .markdown-raw');
      if (code) {{
        navigator.clipboard.writeText(code.textContent).then(() => {{
          const t = document.getElementById('toast');
          t.style.opacity = '1';
          setTimeout(() => t.style.opacity = '0', 2000);
        }});
      }}
    }}
  </script>

  <script>
    // ── Annotation system — text node cache + offset math ───────────────────

    const ANN_FILE = '{ann_file_key}';

    function getAnnotatorName() {{
      let name = localStorage.getItem('fileShareAuthorName');
      if (!name) {{
        name = (prompt('Your name (for annotations):', '') || '').trim() || 'anonymous';
        localStorage.setItem('fileShareAuthorName', name);
      }}
      return name;
    }}

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

    let _textNodeCache = null;
    function getPreviewTextNodes() {{
      if (_textNodeCache) return _textNodeCache;
      const root = document.querySelector('.file-box pre code, .file-box .markdown-body, .file-box pre');
      if (!root) return [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      let node;
      while ((node = walker.nextNode())) nodes.push(node);
      _textNodeCache = nodes;
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
        if (ann.offset_start === null || ann.offset_end === null) continue; // unanchored
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

    // ── FAB + panel rendering ────────────────────────────────────────────────

    // ── TOC builder (markdown only) ────────────────────────────────────────

    // ── Highlight rendering (CSS Custom Highlight API) ─────────────────────

    function buildTOC() {{
      const headers = document.querySelectorAll('.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4, .markdown-body h5, .markdown-body h6');
      if (!headers.length) return;

      const usedSlugs = {{}};
      headers.forEach((h, i) => {{
        const base = h.textContent.toLowerCase()
          .replace(/[^a-zA-Z0-9 _-]/g, '').replace(/ +/g, '-').replace(/^-+|-+$/g, '') || ('h' + i);
        let slug = base;
        let n = 1;
        while (usedSlugs[slug]) slug = base + '-' + (n++);
        usedSlugs[slug] = true;
        h.id = slug;
      }});

      const flyout = document.getElementById('toc-flyout');
      flyout.textContent = '';
      headers.forEach(h => {{
        const level = parseInt(h.tagName[1]);
        const a = document.createElement('a');
        a.className = `toc-item toc-level-${{level}}`;
        a.href = `#${{h.id}}`;
        a.textContent = h.textContent;
        flyout.appendChild(a);
      }});

      const fab = document.getElementById('toc-fab');
      fab.style.display = 'flex';
    }}

    function renderPanel(annotations) {{
      const body = document.getElementById('ann-panel-body');
      if (!body) return;
      const icons = {{ upvote: '👍', downvote: '👎', comment: '💬' }};
      body.textContent = '';
      if (!annotations.length) {{
        const empty = document.createElement('div');
        empty.style.color = '#484f58';
        empty.style.textAlign = 'center';
        empty.style.padding = '20px 0';
        empty.textContent = 'No annotations yet';
        body.appendChild(empty);
        return;
      }}
      // Built via DOM APIs (textContent), not innerHTML string interpolation --
      // selected_text/comment/author are user-supplied and must never be
      // parsed as markup.
      annotations.forEach(a => {{
        const item = document.createElement('div');
        item.className = 'ann-item';

        const textDiv = document.createElement('div');
        textDiv.className = 'ann-item-text';
        if (a.offset_start === null) {{
          const em = document.createElement('em');
          em.textContent = '(not tied to a selection)';
          textDiv.appendChild(em);
        }} else {{
          const snippet = a.selected_text.slice(0, 80) + (a.selected_text.length > 80 ? '…' : '');
          textDiv.textContent = `"${{snippet}}"`;
        }}

        const meta = document.createElement('div');
        meta.className = 'ann-item-meta';

        const typeSpan = document.createElement('span');
        typeSpan.className = 'ann-type-' + a.type;
        typeSpan.textContent = `${{icons[a.type] || ''}} ${{a.type}}`;
        meta.appendChild(typeSpan);

        const authorSpan = document.createElement('span');
        authorSpan.style.color = '#484f58';
        authorSpan.style.fontSize = '11px';
        authorSpan.textContent = a.author;
        meta.appendChild(authorSpan);

        if (a.comment) {{
          const commentSpan = document.createElement('span');
          commentSpan.style.color = '#c9d1d9';
          commentSpan.style.fontSize = '12px';
          commentSpan.textContent = '— ' + a.comment;
          meta.appendChild(commentSpan);
        }}

        const deleteSpan = document.createElement('span');
        deleteSpan.className = 'ann-item-delete';
        deleteSpan.textContent = '✕';
        deleteSpan.addEventListener('click', () => deleteAnnotation(a.id));
        meta.appendChild(deleteSpan);

        item.appendChild(textDiv);
        item.appendChild(meta);
        body.appendChild(item);
      }});
    }}

    let _loading = false;
    async function loadAnnotations() {{
      if (_loading) return;
      _loading = true;
      const body = document.getElementById('ann-panel-body');
      if (body) body.innerHTML = '<div style="color:#484f58;text-align:center;padding:20px 0">Loading…</div>';
      try {{
        const resp = await fetch(`/files-api/annotations?file=${{encodeURIComponent(ANN_FILE)}}`);
        if (!resp.ok) {{ renderPanel([]); return; }}
        _annotations = await resp.json();
        applyHighlights(_annotations);
        renderFab(_annotations);
        renderPanel(_annotations);
      }} catch (e) {{ console.error('Failed to load annotations', e); renderPanel([]); }}
      finally {{ _loading = false; }}
    }}

    // ── Annotation CRUD ─────────────────────────────────────────────────────

    async function deleteAnnotation(annId) {{
      const resp = await fetch(`/files-api/annotations/${{annId}}`, {{ method: 'DELETE' }});
      if (resp.ok) loadAnnotations();
    }}

    // ── Selection capture (pointer-aware) ──────────────────────────────────

    let _pendingSelection = null;
    let _pendingUnanchored = false;

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

    // Resolve a page click point to a character offset in the preview content.
    // Uses caretRangeFromPoint (Chrome/Safari) or caretPositionFromPoint (Firefox).
    function getOffsetFromPoint(x, y) {{
      let range;
      if (document.caretRangeFromPoint) {{
        range = document.caretRangeFromPoint(x, y);
      }} else if (document.caretPositionFromPoint) {{
        const pos = document.caretPositionFromPoint(x, y);
        if (!pos) return null;
        range = document.createRange();
        range.setStart(pos.offsetNode, pos.offset);
      }}
      if (!range) return null;
      return getContentCharOffset(range.startContainer, range.startOffset);
    }}

    // Returns the first annotation whose char range contains offset.
    // When annotations overlap, the earliest-inserted one wins (insertion order).
    function findAnnotationAtOffset(offset) {{
      return _annotations.find(a =>
        a.offset_start !== null && offset >= a.offset_start && offset < a.offset_end) || null;
    }}

    // ── Click-on-highlight detection ─────────────────────────────────────────

    let _detailAnn = null;
    let _detailSaveTimer = null;

    // ── Annotation detail sheet (view/edit) ─────────────────────────────────

    function openAnnotationDetail(ann) {{
      _detailAnn = ann;
      document.getElementById('ann-detail-snippet').textContent =
        '"' + ann.selected_text.slice(0, 120) + (ann.selected_text.length > 120 ? '…' : '') + '"';
      // Set type buttons pressed state
      document.querySelectorAll('.ann-type-btn').forEach(btn => {{
        btn.className = 'ann-type-btn';
        if (btn.dataset.type === ann.type) btn.classList.add('active-' + ann.type);
      }});
      const ta = document.getElementById('ann-detail-comment');
      ta.value = ann.comment || '';
      document.getElementById('ann-detail-saved').classList.remove('visible');
      openSheet('ann-detail-sheet');
    }}

    // Wire content-area click to detect annotation hits.
    // Only fires when selection is collapsed (user is not mid-select).
    (function wireContentClick() {{
      const contentRoot = document.querySelector('.file-box');
      if (!contentRoot) return;
      contentRoot.addEventListener('click', (e) => {{
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed) return;
        const offset = getOffsetFromPoint(e.clientX, e.clientY);
        if (offset === null) return;
        const ann = findAnnotationAtOffset(offset);
        if (ann) {{
          openAnnotationDetail(ann);
          e.stopPropagation();
        }}
      }});
    }})();

    // Track whether a pointer/touch is currently held down.
    // Never show the action bar mid-drag — only after the pointer releases.
    let _pointerActive = false;
    let _pointerTimer = null;
    let _selectionTimer = null;

    // Part A: Delayed pointer activation — allows tap-to-select to complete before blocking
    document.addEventListener('pointerdown', () => {{
      clearTimeout(_pointerTimer);
      _pointerTimer = setTimeout(() => {{ _pointerActive = true; }}, 100);
    }}, {{ passive: true }});
    document.addEventListener('pointerup', () => {{
      _pointerActive = false;
      clearTimeout(_pointerTimer);
      clearTimeout(_selectionTimer);
      _selectionTimer = setTimeout(checkSelection, 80);
    }}, {{ passive: true }});
    document.addEventListener('pointercancel', () => {{
      _pointerActive = false;
      clearTimeout(_pointerTimer);
    }}, {{ passive: true }});
    // touchend is the authoritative "finger actually lifted" signal. Mobile
    // long-press-to-select-word fires pointercancel at gesture-recognition
    // time (when the OS hands off to its native selection UI) -- which can
    // happen well before the user finishes extending a drag-selection, so
    // scheduling the check there fires too early and cuts drags short.
    // touchend only fires once, at real release, so it's safe for both the
    // single-word-tap and the drag-to-extend case.
    document.addEventListener('touchend', () => {{
      clearTimeout(_selectionTimer);
      _selectionTimer = setTimeout(checkSelection, 80);
    }}, {{ passive: true }});

    // Part B: Desktop mouse drag — capture selection immediately on mouseup
    document.addEventListener('mousedown', (e) => {{
      if (e.button === 0) {{
        clearTimeout(_pointerTimer);
        _pointerActive = false;
      }}
    }}, {{ passive: true }});
    document.addEventListener('mouseup', () => {{
      clearTimeout(_selectionTimer);
      _selectionTimer = setTimeout(checkSelection, 50);
    }}, {{ passive: true }});

    function checkSelection() {{
      const capture = captureSelection();
      if (capture) {{
        _pendingSelection = capture;
        openSheet('ann-action-sheet');
      }} else {{
        const sheet = document.getElementById('ann-action-sheet');
        if (sheet && sheet.classList.contains('open') && !_pendingSelection) {{
          closeSheet('ann-action-sheet');
        }}
      }}
    }}

    document.addEventListener('selectionchange', () => {{
      if (_pointerActive) return; // don't interrupt active drag
      clearTimeout(_selectionTimer);
      _selectionTimer = setTimeout(checkSelection, 200);
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
          type, comment, author: getAnnotatorName() }}),
      }});
      if (resp.ok) loadAnnotations();
    }}

    // ── Sheet open/close helpers ─────────────────────────────────────────────

    function openSheet(id) {{
      document.getElementById(id).classList.add('open');
      document.getElementById('ann-backdrop').classList.add('visible');
    }}
    function closeSheet(id) {{
      document.getElementById(id).classList.remove('open');
      document.getElementById('ann-backdrop').classList.remove('visible');
    }}

    // ── DOMContentLoaded — event wiring ─────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {{
      const fab = document.getElementById('ann-fab');
      fab.addEventListener('click', async () => {{
        await loadAnnotations();
        openSheet('ann-panel-sheet');
      }});

      const tocFab = document.getElementById('toc-fab');
      const tocFlyout = document.getElementById('toc-flyout');

      tocFab.addEventListener('click', (e) => {{
        const open = tocFlyout.classList.toggle('open');
        tocFab.classList.toggle('open', open);
        e.stopPropagation();
      }});

      tocFlyout.addEventListener('click', () => {{
        tocFlyout.classList.remove('open');
        tocFab.classList.remove('open');
      }});

      document.addEventListener('click', (e) => {{
        if (!tocFab.contains(e.target) && !tocFlyout.contains(e.target)) {{
          tocFlyout.classList.remove('open');
          tocFab.classList.remove('open');
        }}
      }});

      document.getElementById('ann-panel-close')
        .addEventListener('click', () => closeSheet('ann-panel-sheet'));

      document.getElementById('ann-panel-add-general').addEventListener('click', () => {{
        _pendingSelection = null;
        _pendingUnanchored = true;
        document.getElementById('ann-comment-label').textContent = 'Comment (not tied to a selection)';
        document.getElementById('ann-comment-input').value = '';
        closeSheet('ann-panel-sheet');
        openSheet('ann-comment-sheet');
        setTimeout(() => document.getElementById('ann-comment-input').focus(), 260);
      }});

      document.getElementById('ann-backdrop').addEventListener('click', () => {{
        closeSheet('ann-panel-sheet');
        closeSheet('ann-action-sheet');
        closeSheet('ann-comment-sheet');
        closeSheet('ann-detail-sheet');
        _pendingSelection = null;
        _pendingUnanchored = false;
        _detailAnn = null;
      }});

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
        setTimeout(() => document.getElementById('ann-comment-input').focus(), 260);
      }});

      // Cancel discards the pending selection without saving.
      // mousedown fires before blur, so setting _cmtCancelling prevents
      // the blur handler from auto-saving when the cancel button is tapped.
      let _cmtCancelling = false;
      const _cancelBtn = document.getElementById('ann-comment-cancel');
      _cancelBtn.addEventListener('mousedown', () => {{ _cmtCancelling = true; }});
      _cancelBtn.addEventListener('pointerdown', () => {{ _cmtCancelling = true; }}, {{ passive: true }});
      document.getElementById('ann-comment-cancel').addEventListener('click', () => {{
        _cmtCancelling = false;
        closeSheet('ann-comment-sheet');
        _pendingSelection = null;
        _pendingUnanchored = false;
      }});

      // Auto-save on blur: when the textarea loses focus (e.g. user taps outside
      // or switches focus), save the comment and show a brief "✓ Saved" toast.
      document.getElementById('ann-comment-input').addEventListener('blur', async () => {{
        if (_cmtCancelling || (!_pendingSelection && !_pendingUnanchored)) return;
        const sel = _pendingSelection;
        const unanchored = _pendingUnanchored;
        _pendingSelection = null;
        _pendingUnanchored = false;
        const comment = document.getElementById('ann-comment-input').value.trim();
        closeSheet('ann-comment-sheet');
        const resp = await fetch('/files-api/annotations', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            file: ANN_FILE,
            selected_text: unanchored ? '' : sel.selectedText,
            offset_start: unanchored ? null : sel.offsetStart,
            offset_end: unanchored ? null : sel.offsetEnd,
            type: 'comment',
            comment,
            author: getAnnotatorName(),
          }}),
        }});
        if (resp.ok) {{
          loadAnnotations();
          // Show "✓ Saved" toast briefly
          const t = document.getElementById('toast');
          t.textContent = '✓ Saved';
          t.style.background = '#1f6feb';
          t.style.opacity = '1';
          setTimeout(() => {{
            t.style.opacity = '0';
            setTimeout(() => {{
              t.textContent = 'Copied to clipboard!';
              t.style.background = '#238636';
            }}, 300);
          }}, 1800);
        }}
      }});

      // Detail sheet close
      document.getElementById('ann-detail-close').addEventListener('click', () => {{
        closeSheet('ann-detail-sheet');
        _detailAnn = null;
      }});

      // Type buttons in detail sheet
      document.querySelectorAll('.ann-type-btn').forEach(btn => {{
        btn.addEventListener('click', async () => {{
          const ann = _detailAnn; // snapshot before any await
          if (!ann) return;
          const newType = btn.dataset.type;
          const resp = await fetch(`/files-api/annotations/${{ann.id}}`, {{
            method: 'PATCH',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ type: newType }}),
          }});
          if (resp.ok) {{
            const updated = await resp.json();
            if (_detailAnn && _detailAnn.id === ann.id) {{
              _detailAnn = updated;
              document.querySelectorAll('.ann-type-btn').forEach(b => {{
                b.className = 'ann-type-btn';
                if (b.dataset.type === _detailAnn.type) b.classList.add('active-' + _detailAnn.type);
              }});
            }}
            loadAnnotations();
          }}
        }});
      }});

      // Auto-save comment on blur in detail sheet
      document.getElementById('ann-detail-comment').addEventListener('blur', async () => {{
        const ann = _detailAnn; // snapshot before any await
        if (!ann) return;
        const comment = document.getElementById('ann-detail-comment').value;
        const resp = await fetch(`/files-api/annotations/${{ann.id}}`, {{
          method: 'PATCH',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ comment }}),
        }});
        if (resp.ok) {{
          const updated = await resp.json();
          if (_detailAnn && _detailAnn.id === ann.id) {{
            _detailAnn = updated;
            const saved = document.getElementById('ann-detail-saved');
            saved.classList.add('visible');
            clearTimeout(_detailSaveTimer);
            _detailSaveTimer = setTimeout(() => saved.classList.remove('visible'), 1800);
          }}
          loadAnnotations();
        }}
      }});

      // Delete from detail sheet
      document.getElementById('ann-detail-delete').addEventListener('click', async () => {{
        if (!_detailAnn) return;
        await deleteAnnotation(_detailAnn.id);
        closeSheet('ann-detail-sheet');
        _detailAnn = null;
      }});

      document.addEventListener('keydown', e => {{
        if (e.key === 'Escape') {{
          closeSheet('ann-action-sheet');
          closeSheet('ann-comment-sheet');
          closeSheet('ann-detail-sheet');
          _pendingSelection = null;
          _detailAnn = null;
        }}
      }});

      loadAnnotations();

      // ── Back-to-top ─────────────────────────────────────────────────────────

      // Back-to-top button
      const backToTop = document.getElementById('back-to-top');
      window.addEventListener('scroll', () => {{
        if (window.scrollY > 300) backToTop.classList.add('visible');
        else backToTop.classList.remove('visible');
      }}, {{ passive: true }});
      backToTop.addEventListener('click', () => {{
        window.scrollTo({{ top: 0, behavior: 'smooth' }});
      }});

      // Restore saved panel height from cookie
      const savedPanelH = getCookie('fileshare_panel_h');
      if (savedPanelH) document.getElementById('ann-panel-sheet').style.setProperty('--panel-h', savedPanelH);

      // Drag-to-resize the panel sheet by dragging its top handle
      const _panelSheet = document.getElementById('ann-panel-sheet');
      const _panelHandle = _panelSheet.querySelector('.ann-sheet-handle');
      let _panelResizing = false;
      let _panelResizeStartY = 0;
      let _panelResizeStartH = 0;

      _panelHandle.addEventListener('pointerdown', (e) => {{
        _panelResizing = true;
        _panelResizeStartY = e.clientY;
        _panelResizeStartH = _panelSheet.getBoundingClientRect().height;
        _panelHandle.setPointerCapture(e.pointerId);
        e.preventDefault();
      }});

      _panelHandle.addEventListener('pointermove', (e) => {{
        if (!_panelResizing) return;
        const delta = _panelResizeStartY - e.clientY; // drag up = larger
        const newH = Math.min(
          Math.max(_panelResizeStartH + delta, window.innerHeight * 0.25),
          window.innerHeight * 0.92
        );
        _panelSheet.style.setProperty('--panel-h', newH + 'px');
      }});

      _panelHandle.addEventListener('pointerup', () => {{
        if (!_panelResizing) return;
        _panelResizing = false;
        setCookie('fileshare_panel_h', _panelSheet.style.getPropertyValue('--panel-h'), 365);
      }});

      _panelHandle.addEventListener('pointercancel', () => {{
        _panelResizing = false;
      }});
    }});
  </script>
</body>
</html>
'''

# ── 6. Directory listing template ──────────────────────────
DIR_HTML_TEMPLATE = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Index of {path}</title>
  <script>
    (function() {{
      var t = localStorage.getItem('fileshare_theme') || 'dark';
      document.documentElement.setAttribute('data-theme', t);
    }})();
  </script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    :root {{
      --bg: #0d1117;
      --surface: #161b22;
      --surface-2: #21262d;
      --fg: #c9d1d9;
      --fg-strong: #e6edf3;
      --fg-muted: #8b949e;
      --border: #30363d;
      --border-soft: #21262d;
      --link: #58a6ff;
      --row-hover: #1c2129;
      --accent: #1f6feb;
      --accent-fg: #ffffff;
    }}
    :root[data-theme="light"] {{
      --bg: #ffffff;
      --surface: #ffffff;
      --surface-2: #f6f8fa;
      --fg: #1f2328;
      --fg-strong: #1f2328;
      --fg-muted: #57606a;
      --border: #d0d7de;
      --border-soft: #eaeef2;
      --link: #0969da;
      --row-hover: #f6f8fa;
      --accent: #0969da;
      --accent-fg: #ffffff;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
    }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .header {{
      position: relative;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 24px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      font-size: 16px;
      font-weight: 600;
      color: var(--fg-strong);
    }}
    .options-btn {{
      background: transparent;
      border: none;
      font-size: 18px;
      line-height: 1;
      cursor: pointer;
      color: var(--fg-muted);
      padding: 4px 8px;
      border-radius: 6px;
      flex-shrink: 0;
    }}
    .options-btn:hover {{ background: var(--row-hover); color: var(--fg-strong); }}
    .options-flyout {{
      display: none;
      position: absolute;
      top: 100%;
      right: 20px;
      margin-top: 8px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
      z-index: 20;
      min-width: 220px;
      font-size: 13px;
      font-weight: 400;
    }}
    .options-flyout.open {{ display: block; }}
    .options-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .options-row:last-child {{ margin-bottom: 0; }}
    .options-label {{ color: var(--fg-muted); }}
    .options-toggle {{
      display: flex;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }}
    .options-toggle button {{
      background: var(--surface-2);
      color: var(--fg-muted);
      border: none;
      border-right: 1px solid var(--border);
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
    }}
    .options-toggle button:last-child {{ border-right: none; }}
    .options-toggle button.active {{ background: var(--accent); color: var(--accent-fg); }}
    #default-sort-select {{
      background: var(--surface-2);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
    }}
    .content {{ max-width: 900px; margin: 24px auto; padding: 0 20px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }}
    th {{
      background: var(--surface-2);
      padding: 10px 16px;
      text-align: left;
      color: var(--fg-muted);
      font-weight: 500;
      border-bottom: 1px solid var(--border);
    }}
    td {{
      padding: 8px 16px;
      border-bottom: 1px solid var(--border-soft);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: var(--row-hover); }}
    .icon {{ margin-right: 6px; }}
    .name {{ overflow-wrap: anywhere; }}
    .size {{ color: var(--fg-muted); text-align: right; white-space: nowrap; width: 1%; }}
    .mtime {{ color: var(--fg-muted); text-align: right; white-space: nowrap; width: 1%; }}
    th[data-key] {{ cursor: pointer; user-select: none; }}
    th[data-key]:hover {{ color: var(--fg); }}
    .sort-icon {{ display: inline-block; width: 1em; }}
  </style>
</head>
<body>
  <div class="header">
    <span>Index of {path}</span>
    <button class="options-btn" id="options-btn" title="Display options" aria-label="Display options">⚙️</button>
    <div class="options-flyout" id="options-flyout">
      <div class="options-row">
        <span class="options-label">Theme</span>
        <div class="options-toggle" id="theme-toggle">
          <button data-value="dark">Dark</button>
          <button data-value="light">Light</button>
        </div>
      </div>
      <div class="options-row">
        <span class="options-label">Timestamps</span>
        <div class="options-toggle" id="time-toggle">
          <button data-value="exact">Exact</button>
          <button data-value="relative">Relative</button>
        </div>
      </div>
      <div class="options-row">
        <span class="options-label">Default sort</span>
        <select id="default-sort-select">
          <option value="name">Name</option>
          <option value="size">Size</option>
          <option value="mtime">Last Modified</option>
        </select>
      </div>
    </div>
  </div>
  <div class="content">
    <table>
      <thead>
        <tr>
          <th data-key="name">Name <span class="sort-icon" data-col="name"></span></th>
          <th data-key="size" class="size">Size <span class="sort-icon" data-col="size"></span></th>
          <th data-key="mtime" class="mtime">Last Modified <span class="sort-icon" data-col="mtime"></span></th>
        </tr>
      </thead>
      <tbody id="dir-rows"></tbody>
    </table>
  </div>

  <script>
    const ENTRIES = {entries_json};
    const HAS_PARENT = {has_parent};
    const PARENT_HREF = {parent_href};

    function getPref(key, fallback) {{
      const v = localStorage.getItem(key);
      return v === null ? fallback : v;
    }}
    function setPref(key, val) {{ localStorage.setItem(key, val); }}

    let theme = getPref('fileshare_theme', 'dark');
    let useRelativeTime = getPref('fileshare_relative_time', 'false') === 'true';
    let sortKey = getPref('fileshare_sort_key', 'mtime');
    let sortDir = sortKey === 'name' ? 'asc' : 'desc';

    function fmtSize(bytes) {{
      if (bytes === null || bytes === undefined) return '—';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0, v = bytes;
      while (v >= 1024 && i < units.length - 1) {{ v /= 1024; i++; }}
      return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
    }}

    function fmtRelative(date) {{
      const sec = Math.round((Date.now() - date.getTime()) / 1000);
      const units = [
        [60, 'second'], [60, 'minute'], [24, 'hour'], [7, 'day'],
        [4.345, 'week'], [12, 'month'], [Infinity, 'year'],
      ];
      let value = sec, name = 'second';
      for (const [size, unitName] of units) {{
        if (value < size) {{ name = unitName; break; }}
        value /= size;
        name = unitName;
      }}
      value = Math.round(value);
      if (name === 'second') return 'just now';
      return `${{value}} ${{name}}${{value === 1 ? '' : 's'}} ago`;
    }}

    function fmtMtime(iso) {{
      if (!iso) return '—';
      const d = new Date(iso);
      return useRelativeTime ? fmtRelative(d) : d.toLocaleString();
    }}

    const IMAGE_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico']);
    const VIDEO_EXTS = new Set(['mp4', 'webm', 'mkv', 'avi', 'mov']);
    const DOC_EXTS = new Set(['txt', 'md']);
    const CODE_EXTS = new Set([
      'py', 'sh', 'bash', 'zsh', 'js', 'ts', 'tsx', 'jsx', 'yaml', 'yml',
      'toml', 'ini', 'cfg', 'conf', 'css', 'html', 'htm', 'sql', 'rs', 'go',
      'c', 'cpp', 'h', 'java', 'rb', 'lua', 'json', 'xml',
    ]);

    function fileIcon(name) {{
      const dot = name.lastIndexOf('.');
      const ext = dot === -1 ? '' : name.slice(dot + 1).toLowerCase();
      if (IMAGE_EXTS.has(ext)) return '🖼️';
      if (VIDEO_EXTS.has(ext)) return '🎬';
      if (CODE_EXTS.has(ext)) return '💻';
      if (DOC_EXTS.has(ext)) return '📝';
      return '📄';
    }}

    function makeRow(entry) {{
      const tr = document.createElement('tr');

      const nameTd = document.createElement('td');
      nameTd.className = 'name';
      const iconSpan = document.createElement('span');
      iconSpan.className = 'icon';
      iconSpan.textContent = entry.isDir ? '📁' : fileIcon(entry.name);
      const a = document.createElement('a');
      a.href = entry.href;
      a.textContent = entry.name;
      nameTd.appendChild(iconSpan);
      nameTd.appendChild(a);

      const sizeTd = document.createElement('td');
      sizeTd.className = 'size';
      sizeTd.textContent = fmtSize(entry.size);

      const mtimeTd = document.createElement('td');
      mtimeTd.className = 'mtime';
      mtimeTd.textContent = fmtMtime(entry.mtime);
      if (useRelativeTime && entry.mtime) mtimeTd.title = new Date(entry.mtime).toLocaleString();

      tr.appendChild(nameTd);
      tr.appendChild(sizeTd);
      tr.appendChild(mtimeTd);
      return tr;
    }}

    function render() {{
      const sorted = ENTRIES.slice().sort((a, b) => {{
        let av = a[sortKey], bv = b[sortKey];
        if (sortKey === 'name') {{ av = av.toLowerCase(); bv = bv.toLowerCase(); }}
        if (av === null || av === undefined) av = sortKey === 'name' ? '' : -Infinity;
        if (bv === null || bv === undefined) bv = sortKey === 'name' ? '' : -Infinity;
        if (av < bv) return sortDir === 'asc' ? -1 : 1;
        if (av > bv) return sortDir === 'asc' ? 1 : -1;
        return 0;
      }});

      const tbody = document.getElementById('dir-rows');
      tbody.textContent = '';

      if (HAS_PARENT) {{
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.className = 'name';
        const iconSpan = document.createElement('span');
        iconSpan.className = 'icon';
        iconSpan.textContent = '📁';
        const a = document.createElement('a');
        a.href = PARENT_HREF;
        a.textContent = '..';
        td.appendChild(iconSpan);
        td.appendChild(a);
        tr.appendChild(td);
        tr.appendChild(document.createElement('td'));
        tr.appendChild(document.createElement('td'));
        tbody.appendChild(tr);
      }}

      sorted.forEach(entry => tbody.appendChild(makeRow(entry)));

      document.querySelectorAll('.sort-icon').forEach(el => {{
        const col = el.dataset.col;
        el.textContent = col === sortKey ? (sortDir === 'asc' ? '▲' : '▼') : '';
      }});
    }}

    document.querySelectorAll('th[data-key]').forEach(th => {{
      th.addEventListener('click', () => {{
        const key = th.dataset.key;
        if (sortKey === key) {{
          sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        }} else {{
          sortKey = key;
          sortDir = key === 'name' ? 'asc' : 'desc';
        }}
        render();
      }});
    }});

    // ── Options flyout ──────────────────────────────────────────────────────

    function setActiveToggle(container, value) {{
      container.querySelectorAll('button').forEach(b => {{
        b.classList.toggle('active', b.dataset.value === value);
      }});
    }}

    const optionsBtn = document.getElementById('options-btn');
    const optionsFlyout = document.getElementById('options-flyout');
    optionsBtn.addEventListener('click', (e) => {{
      e.stopPropagation();
      optionsFlyout.classList.toggle('open');
    }});
    document.addEventListener('click', (e) => {{
      if (!optionsFlyout.contains(e.target) && !optionsBtn.contains(e.target)) {{
        optionsFlyout.classList.remove('open');
      }}
    }});

    document.querySelectorAll('#theme-toggle button').forEach(b => {{
      b.addEventListener('click', () => {{
        theme = b.dataset.value;
        setPref('fileshare_theme', theme);
        document.documentElement.setAttribute('data-theme', theme);
        setActiveToggle(document.getElementById('theme-toggle'), theme);
      }});
    }});

    document.querySelectorAll('#time-toggle button').forEach(b => {{
      b.addEventListener('click', () => {{
        useRelativeTime = b.dataset.value === 'relative';
        setPref('fileshare_relative_time', useRelativeTime ? 'true' : 'false');
        setActiveToggle(document.getElementById('time-toggle'), b.dataset.value);
        render();
      }});
    }});

    document.getElementById('default-sort-select').addEventListener('change', (e) => {{
      sortKey = e.target.value;
      sortDir = sortKey === 'name' ? 'asc' : 'desc';
      setPref('fileshare_sort_key', sortKey);
      render();
    }});

    setActiveToggle(document.getElementById('theme-toggle'), theme);
    setActiveToggle(document.getElementById('time-toggle'), useRelativeTime ? 'relative' : 'exact');
    document.getElementById('default-sort-select').value = sortKey;
    render();
  </script>
</body>
</html>
'''


# ── 7. HTTP handler ───────────────────────────────────────
class GistHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f'[file-share] {self.address_string()} - {format % args}', flush=True)

    def send_error_page(self, code, message):
        body = f'<html><body><h2>{code} {message}</h2></body></html>'.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Minimal HEAD support: just return headers for files
        parsed = urllib.parse.urlparse(self.path)
        url_path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw_mode = '1' in query.get('raw', [])

        if url_path.startswith(PREFIX + '/'):
            rel_path = url_path[len(PREFIX):]
        elif url_path in (PREFIX, PREFIX + '/'):
            rel_path = '/'
        else:
            self.send_error_page(404, 'Not Found')
            return

        fs_path = SHARED_DIR / rel_path.lstrip('/')
        try:
            fs_path = fs_path.resolve()
            fs_path.relative_to(SHARED_DIR.resolve())
        except (ValueError, RuntimeError):
            self.send_error_page(403, 'Forbidden')
            return

        if not fs_path.exists():
            self.send_error_page(404, 'Not Found')
            return

        if fs_path.is_dir():
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            return

        mime, _ = mimetypes.guess_type(str(fs_path))
        if not mime:
            mime = 'application/octet-stream'
        size = fs_path.stat().st_size

        self.send_response(200)
        if raw_mode:
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(size))
        else:
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()

    def _send_json(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_limited_body(self) -> bytes | None:
        """Read the request body, rejecting it before buffering into memory
        if Content-Length is missing/invalid or exceeds MAX_UPLOAD_BYTES.

        Sends the error response itself; callers should return immediately
        when this returns None. Closes the connection in that case since
        any unread body bytes would otherwise be misparsed as the start of
        the next keep-alive request.
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(400, {'error': 'Invalid Content-Length'})
            self.close_connection = True
            return None
        if length > MAX_UPLOAD_BYTES:
            self._send_json(413, {'error': f'Request body exceeds {MAX_UPLOAD_BYTES} byte limit'})
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b''

    def _read_json_body(self) -> dict | None:
        """Returns the parsed body, or None if a response was already sent
        (body too large, or invalid JSON) -- callers should just return."""
        body = self._read_limited_body()
        if body is None:
            return None
        if not body:
            return {}
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {'error': 'Invalid JSON'})
            return None

    def _handle_create_blob(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        filename = query.get('filename', [None])[0]
        if not filename or not is_valid_upload_filename(filename):
            self._send_json(400, {'error': (
                'filename must be non-empty and must not contain "/", "..", '
                'a NUL byte, or start with "."'
            )})
            return

        body = self._read_limited_body()
        if body is None:
            return

        fs_path = None
        for _ in range(5):
            blob_id = generate_blob_id()
            if find_blob_path(blob_id) is None:
                fs_path = SHARED_DIR / f'{blob_id}-{filename}'
                break
        if fs_path is None:
            self._send_json(500, {'error': 'Could not allocate a unique blob id'})
            return

        fs_path.write_bytes(body)
        self._send_json(201, blob_metadata(fs_path))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/files-api/blobs':
            self._handle_create_blob(parsed)
            return

        if parsed.path != '/files-api/annotations':
            self._send_json(404, {'error': 'Not Found'})
            return

        body = self._read_json_body()
        if body is None:
            return

        required = {'file', 'type'}
        missing = required - body.keys()
        if missing:
            self._send_json(400, {'error': f'Missing fields: {sorted(missing)}'})
            return

        if body['type'] not in ('upvote', 'downvote', 'comment'):
            self._send_json(400, {'error': 'type must be upvote, downvote, or comment'})
            return

        # offset_start/offset_end are optional -- omitted or null means an
        # "unanchored" annotation (a general comment on the file as a whole,
        # not tied to a text range, e.g. a comment on an image).
        offset_start = body.get('offset_start')
        offset_end = body.get('offset_end')
        if (offset_start is None) != (offset_end is None):
            self._send_json(400, {'error': 'offset_start and offset_end must both be set or both omitted/null'})
            return

        ann = _store.add(
            file=str(body['file']),
            selected_text=str(body.get('selected_text', '')),
            offset_start=int(offset_start) if offset_start is not None else None,
            offset_end=int(offset_end) if offset_end is not None else None,
            ann_type=str(body['type']),
            comment=str(body.get('comment', '')),
            author=str(body.get('author', 'anonymous')),
        )
        self._send_json(201, ann)

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

        body = self._read_limited_body()
        if body is None:
            return
        fs_path.write_bytes(body)

        drained = _store.delete_by_file(blob_id)
        meta = blob_metadata(fs_path)
        meta['drained_annotations'] = drained
        self._send_json(200, meta)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip('/').split('/')
        if len(parts) != 3 or parts[:2] != ['files-api', 'annotations']:
            self._send_json(404, {'error': 'Not Found'})
            return
        ann_id = parts[2]
        body = self._read_json_body()
        if not isinstance(body, dict):
            self._send_json(400, {'error': 'Invalid JSON'})
            return
        ann_type = body.get('type')
        if ann_type is not None and ann_type not in ('upvote', 'downvote', 'comment'):
            self._send_json(400, {'error': 'type must be upvote, downvote, or comment'})
            return
        updated = _store.update(
            ann_id,
            comment=body.get('comment'),
            ann_type=ann_type,
        )
        if updated:
            self._send_json(200, updated)
        else:
            self._send_json(404, {'error': 'Annotation not found'})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        url_path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw_mode = '1' in query.get('raw', [])

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
        if url_path.startswith(PREFIX + '/'):
            rel_path = url_path[len(PREFIX):]  # e.g. /a1b2c3d4-example.md
        elif url_path == PREFIX or url_path == PREFIX + '/':
            rel_path = '/'
        else:
            self.send_error_page(404, 'Not Found')
            return

        # Resolve to filesystem path
        # rel_path starts with /
        fs_path = SHARED_DIR / rel_path.lstrip('/')

        # Security: prevent path traversal
        try:
            fs_path = fs_path.resolve()
            shared_resolved = SHARED_DIR.resolve()
            fs_path.relative_to(shared_resolved)
        except (ValueError, RuntimeError):
            self.send_error_page(403, 'Forbidden')
            return

        if not fs_path.exists():
            self.send_error_page(404, 'Not Found')
            return

        if fs_path.is_dir():
            self.serve_directory(fs_path, url_path)
            return

        if raw_mode:
            self.serve_raw(fs_path)
        else:
            self.serve_preview(fs_path, url_path)

    def serve_raw(self, fs_path):
        mime, encoding = mimetypes.guess_type(str(fs_path))
        if not mime:
            mime = 'application/octet-stream'
        size = fs_path.stat().st_size
        filename = fs_path.name

        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        self.end_headers()

        with open(fs_path, 'rb') as f:
            self.wfile.write(f.read())

    def serve_directory(self, fs_path, url_path):
        # Ensure trailing slash
        if not url_path.endswith('/'):
            self.send_response(301)
            self.send_header('Location', url_path + '/')
            self.end_headers()
            return

        entries = sorted(fs_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))

        parent_href = ''
        if url_path.rstrip('/') != PREFIX:
            parent_href = url_path.rstrip('/').rsplit('/', 1)[0] + '/'

        entries_data = []
        for entry in entries:
            parsed = parse_blob_name(entry.name) if entry.is_file() else None
            display_name = parsed[1] if parsed else entry.name
            is_dir = entry.is_dir()
            href = url_path + entry.name + ('/' if is_dir else '')
            try:
                st = entry.stat()
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                size = None if is_dir else st.st_size
            except OSError:
                mtime = None
                size = None
            entries_data.append({
                'name': display_name,
                'href': href,
                'isDir': is_dir,
                'size': size,
                'mtime': mtime,
            })

        path_display = html.escape(url_path)
        body = DIR_HTML_TEMPLATE.format(
            path=path_display,
            has_parent='true' if parent_href else 'false',
            parent_href=json.dumps(parent_href),
            entries_json=json.dumps(entries_data),
        ).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def build_breadcrumb(self, url_path):
        """Build a breadcrumb trail for the URL path."""
        rel_path = url_path[len(PREFIX):] if url_path.startswith(PREFIX) else url_path
        parts = rel_path.strip('/').split('/')
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

    def serve_preview(self, fs_path, url_path):
        stat = fs_path.stat()
        size = stat.st_size
        parsed_blob = parse_blob_name(fs_path.name)
        filename = parsed_blob[1] if parsed_blob else fs_path.name

        mime, _ = mimetypes.guess_type(str(fs_path))
        if not mime:
            mime = 'application/octet-stream'

        raw_url = html.escape(url_path + '?raw=1')
        preview_type = get_preview_type(fs_path)

        # Build preview content block
        if preview_type == 'markdown':
            try:
                text = fs_path.read_text(errors='replace')
                escaped = html.escape(text)
                preview_content = (
                    f'<div class="markdown-body" id="md-rendered"></div>'
                    f'<script class="markdown-raw" type="text/plain" id="md-source">{escaped}</script>'
                    f'<script>document.getElementById("md-rendered").innerHTML = '
                    f'marked.parse(document.getElementById("md-source").textContent);'
                    f'if (typeof buildTOC === "function") buildTOC();'
                    f'</script>'
                )
            except Exception:
                preview_content = '<div class="no-preview"><div class="icon">⚠️</div>Could not read file.</div>'

        elif preview_type == 'code':
            lang = get_highlight_lang(fs_path)
            try:
                text = fs_path.read_text(errors='replace')
                if lang == 'json':
                    import json as _json
                    try:
                        text = _json.dumps(_json.loads(text), indent=2, ensure_ascii=False)
                    except Exception:
                        pass
                elif lang == 'xml':
                    try:
                        import xml.dom.minidom as _minidom
                        pretty = _minidom.parseString(text).toprettyxml(indent='  ')
                        # toprettyxml leaves blank lines between text-only nodes;
                        # strip them so already-formatted XML doesn't get sparser.
                        text = '\n'.join(line for line in pretty.splitlines() if line.strip())
                    except Exception:
                        pass
                escaped = html.escape(text)
                preview_content = f'<pre><code class="language-{lang}">{escaped}</code></pre>'
            except Exception:
                preview_content = '<div class="no-preview"><div class="icon">⚠️</div>Could not read file.</div>'

        elif preview_type == 'text':
            try:
                text = fs_path.read_text(errors='replace')
                escaped = html.escape(text)
                preview_content = f'<pre><code class="language-plaintext">{escaped}</code></pre>'
            except Exception:
                preview_content = '<div class="no-preview"><div class="icon">⚠️</div>Could not read file.</div>'

        elif preview_type == 'image':
            preview_content = (
                f'<div class="media-box">'
                f'<img src="{raw_url}" alt="{html.escape(filename)}">'
                f'</div>'
            )

        elif preview_type == 'audio':
            preview_content = (
                f'<div class="media-box">'
                f'<audio controls><source src="{raw_url}"><p>Your browser does not support audio.</p></audio>'
                f'</div>'
            )

        elif preview_type == 'video':
            preview_content = (
                f'<div class="media-box">'
                f'<video controls><source src="{raw_url}"><p>Your browser does not support video.</p></video>'
                f'</div>'
            )

        else:  # binary
            preview_content = (
                f'<div class="no-preview">'
                f'<div class="icon">📦</div>'
                f'<p>No preview available for this file type.</p>'
                f'<a class="btn" href="{raw_url}" download="{html.escape(filename)}" style="display:inline-block;margin-top:8px;padding:6px 16px;background:#238636;color:#fff;border-radius:6px;text-decoration:none;">Download</a>'
                f'</div>'
            )

        # Copy and wrap buttons (only for text/code/markdown types)
        if preview_type in ('markdown', 'code', 'text'):
            copy_btn = '<button class="btn" onclick="copyContent()"><span class="material-icons" style="font-size:16px;vertical-align:-3px;margin-right:4px">content_copy</span>Copy</button>'
            wrap_btn = '<button id="wrapBtn" class="btn" onclick="toggleWrap()"><span class="material-icons" style="font-size:16px;vertical-align:-3px;margin-right:4px">wrap_text</span>Wrap</button>'
        else:
            copy_btn = ''
            wrap_btn = ''

        breadcrumb = self.build_breadcrumb(url_path)
        ann_file_key = parsed_blob[0] if parsed_blob else url_path[len(PREFIX):]

        page = PREVIEW_HTML_TEMPLATE.format(
            title=html.escape(filename),
            filename=html.escape(filename),
            size=format_size(size),
            mime=html.escape(mime),
            raw_url=raw_url,
            copy_btn=copy_btn,
            wrap_btn=wrap_btn,
            breadcrumb=breadcrumb,
            preview_content=preview_content,
            ann_file_key=html.escape(ann_file_key),
        ).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.end_headers()
        self.wfile.write(page)


# ── 8. Entry point ─────────────────────────────────────────
if __name__ == '__main__':
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    handler = GistHandler
    with ThreadingHTTPServer(('127.0.0.1', PORT), handler) as httpd:
        print(f'[file-share] Serving {SHARED_DIR} on 127.0.0.1:{PORT} at prefix {PREFIX}/', flush=True)
        httpd.serve_forever()
