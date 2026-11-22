"""Local workspace shared by the sidebar and Agent file tools.

UTF-8 text only; the journal belongs to this workspace, never to user memory.
"""
from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from functools import lru_cache
from contextlib import contextmanager

MAX_BYTES = 64 * 1024
MAX_CHANGES = 200
PRIVATE_DIR = '.mokoa-workspace'
BLOCKED = {'.git', '.clerk', PRIVATE_DIR, '.venv', 'venv', 'node_modules',
           '__pycache__', '.ssh', '.aws', '.azure', '.tools', '.work'}


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def workspace_enabled() -> bool:
    # A sidebar does not silently grant filesystem access to existing API clients.
    return os.getenv('AGENT_ENABLE_WORKSPACE', os.getenv('AGENT_ENABLE_TOOLS', '')).lower() in {'1', 'true'}


def revision(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()

    def resolve(self, path: str = '.') -> Path:
        if not isinstance(path, str) or not path or '\0' in path:
            raise WorkspaceError('文件路径无效。')
        candidate = Path(path.replace('\\', '/'))
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            parts = candidate.relative_to(self.root).parts
        except ValueError:
            raise WorkspaceError('拒绝访问沙箱目录之外的路径。', 403)
        current = self.root
        for part in parts:
            low = part.lower()
            if (part == '..' or ':' in part or part.rstrip(' .') != part
                    or low in BLOCKED or low.startswith('.mokoa-write-')
                    or low.startswith('.env') and low != '.env.example'
                    or low in {'id_rsa', 'id_ed25519', 'credentials', '.npmrc', '.pypirc'}
                    or low.endswith(('.pem', '.key', '.pfx', '.p12', '.db', '.sqlite', '.sqlite3',
                                     '.db-wal', '.db-shm', '.sqlite-wal', '.sqlite-shm', '.sqlite3-wal', '.sqlite3-shm'))
                    or low.split('.')[0] in {'con', 'prn', 'aux', 'nul', *[f'com{i}' for i in range(1, 10)], *[f'lpt{i}' for i in range(1, 10)]}):
                raise WorkspaceError('路径受保护或超出工作区范围。', 403)
            current = current / part
            if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
                raise WorkspaceError('工作区不跟随符号链接或目录联接。', 403)
        if not current.resolve().is_relative_to(self.root):
            raise WorkspaceError('拒绝访问沙箱目录之外的路径。', 403)
        if current.is_file() and current.stat().st_nlink > 1:
            raise WorkspaceError('工作区不操作硬链接文件。', 403)
        return current

    def relative(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()

    def _bytes(self, target: Path) -> bytes | None:
        if not target.exists():
            return None
        if not target.is_file():
            raise WorkspaceError('目标不是普通文件；不支持删除目录。')
        with target.open('rb') as fh:
            data = fh.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise WorkspaceError('文件超过 64 KiB，请用本地编辑器打开。', 413)
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            raise WorkspaceError('仅支持 UTF-8 文本文件。', 415)
        if b'\0' in data:
            raise WorkspaceError('不支持二进制文件。', 415)
        return data

    def read(self, path: str) -> dict:
        with self.lock:
            target = self.resolve(path)
            data = self._bytes(target)
            if data is None:
                raise WorkspaceError('文件不存在。', 404)
            return {'path': self.relative(target), 'content': data.decode('utf-8'),
                    'revision': revision(data), 'bytes': len(data)}

    def listing(self, path='.', offset=0, limit=100) -> dict:
        with self.lock:
            target = self.resolve(path)
            if not target.is_dir():
                raise WorkspaceError('目录不存在。', 404)
            # Directory-local scan only; never recursively crawl the workspace.
            entries = []
            for child in target.iterdir():
                try:
                    child = self.resolve(self.relative(child))
                except WorkspaceError:
                    continue
                if child.is_dir() or child.is_file():
                    entries.append({'name': child.name, 'path': self.relative(child),
                                    'kind': 'directory' if child.is_dir() else 'file'})
            entries.sort(key=lambda e: (e['kind'] != 'directory', e['name'].lower()))
            return {'path': self.relative(target), 'entries': entries[offset:offset+limit],
                    'total': len(entries), 'next_offset': offset+limit if offset+limit < len(entries) else None}

    def _state_file(self, name):
        folder = self.root / PRIVATE_DIR
        if folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
            raise WorkspaceError('工作区记录目录无效。', 403)
        folder.mkdir(exist_ok=True, mode=0o700)
        db = folder / name
        if db.is_symlink() or db.exists() and db.stat().st_nlink > 1:
            raise WorkspaceError('工作区记录文件无效。', 403)
        return db

    def _journal(self):
        conn = sqlite3.connect(self._state_file('changes.sqlite3'), timeout=5)
        conn.execute('CREATE TABLE IF NOT EXISTS changes (path TEXT PRIMARY KEY, original BLOB, latest TEXT, mode INTEGER, trash BLOB, trash_mode INTEGER)')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(changes)')}
        for name, kind in [('mode', 'INTEGER'), ('trash', 'BLOB'), ('trash_mode', 'INTEGER')]:
            if name not in columns:
                conn.execute(f'ALTER TABLE changes ADD COLUMN {name} {kind}')
        conn.commit()
        return conn

    @contextmanager
    def _writer(self):
        # A separate SQLite lock serializes API workers as well as Agent threads,
        # while the recovery journal can commit before the filesystem mutation.
        conn = sqlite3.connect(self._state_file('writer.sqlite3'), timeout=5)
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield
        except sqlite3.OperationalError:
            raise WorkspaceError('另一个工作区操作尚未结束，请刷新后重试。', 409)
        finally:
            conn.rollback()
            conn.close()

    def _replace(self, target: Path, data: bytes | None, mode: int | None = None):
        # Recheck after creating parents; no directory removal or shell operations.
        if data is None:
            self.resolve(self.relative(target)).unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        self.resolve(self.relative(target))
        fd, tmp = tempfile.mkstemp(prefix='.mokoa-write-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            permissions = mode if mode is not None else target.stat().st_mode if target.exists() else None
            if permissions is not None:
                os.chmod(tmp, permissions)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _mutate(self, path: str, data: bytes | None, expected: str | None) -> dict:
        with self.lock, self._writer():
            target = self.resolve(path)
            before = self._bytes(target)
            if revision(before) != expected:
                raise WorkspaceError('文件已被修改，请重新读取后比较；未覆盖磁盘内容。', 409)
            if data is None and before is None:
                raise WorkspaceError('文件不存在。', 404)
            if before == data:
                return {'path': self.relative(target), 'revision': revision(data), 'changed': False}
            rel = self.relative(target)
            conn = self._journal()
            try:
                row = conn.execute('SELECT original, latest, mode FROM changes WHERE path=?', (rel,)).fetchone()
                if row is None and conn.execute('SELECT count(*) FROM changes').fetchone()[0] >= MAX_CHANGES:
                    raise WorkspaceError('变更记录已达 200 个文件上限，请先恢复或接受已有变更。', 409)
                original = row[0] if row else before
                mode = row[2] if row else target.stat().st_mode if before is not None else None
                # Persist recovery bytes BEFORE touching the user's file.
                trash = before if data is None else None
                trash_mode = target.stat().st_mode if data is None else None
                conn.execute('INSERT OR REPLACE INTO changes (path, original, latest, mode, trash, trash_mode) VALUES (?, ?, ?, ?, ?, ?)',
                             (rel, original, revision(data), mode, trash, trash_mode))
                conn.commit()
                self._replace(target, data)
            finally:
                conn.close()
            return {'path': rel, 'revision': revision(data), 'changed': True, 'deleted': data is None}

    def write(self, path: str, content: str, expected: str | None) -> dict:
        if not isinstance(content, str):
            raise WorkspaceError('content 必须是字符串。')
        data = content.encode('utf-8')
        if len(data) > MAX_BYTES or '\0' in content:
            raise WorkspaceError('仅支持不超过 64 KiB 的 UTF-8 文本。', 413)
        return self._mutate(path, data, expected)

    def delete(self, path: str, expected: str) -> dict:
        if not expected:
            raise WorkspaceError('删除前必须读取文件版本。')
        return self._mutate(path, None, expected)

    def changes(self) -> list[dict]:
        with self.lock:
            if not (self.root / PRIVATE_DIR).exists():
                return []
            conn = self._journal()
            try:
                rows = conn.execute('SELECT path, original, latest, trash FROM changes ORDER BY path').fetchall()
            finally:
                conn.close()
            result = []
            for path, original, latest, trash in rows:
                try:
                    current = self._bytes(self.resolve(path))
                    conflict = revision(current) != latest
                    baseline = trash if current is None and trash is not None else original
                    before = (baseline or b'').decode('utf-8').splitlines(keepends=True)
                    after = (current or b'').decode('utf-8').splitlines(keepends=True)
                    ops = difflib.SequenceMatcher(None, before, after).get_opcodes()
                    added = sum(j2-j1 for tag, i1, i2, j1, j2 in ops if tag in {'replace', 'insert'})
                    removed = sum(i2-i1 for tag, i1, i2, j1, j2 in ops if tag in {'replace', 'delete'})
                    result.append({'path': path, 'status': 'deleted' if current is None else 'added' if original is None else 'modified',
                                   'added': added, 'removed': removed, 'conflict': conflict,
                                   'revision': revision(current)})
                except WorkspaceError as exc:
                    result.append({'path': path, 'status': 'unavailable', 'conflict': True, 'error': str(exc)})
            return result

    def diff(self, path: str) -> dict:
        with self.lock:
            target = self.resolve(path)
            current = self._bytes(target)
            conn = self._journal()
            try:
                row = conn.execute('SELECT original, latest, trash FROM changes WHERE path=?', (self.relative(target),)).fetchone()
            finally:
                conn.close()
            if row is None:
                raise WorkspaceError('没有此文件的工作区变更记录。', 404)
            original, latest, trash = row
            baseline = trash if current is None and trash is not None else original
            raw = difflib.unified_diff((baseline or b'').decode('utf-8').splitlines(keepends=True),
                (current or b'').decode('utf-8').splitlines(keepends=True), fromfile='a/'+path, tofile='b/'+path, lineterm='')
            lines = []
            for i, line in enumerate(raw):
                lines.append(line.rstrip('\r\n'))
                if i > 1 and not line.startswith('@@') and not line.endswith(('\r', '\n')):
                    lines.append('\\ No newline at end of file')
            diff = '\n'.join(lines)
            return {'path': self.relative(target), 'diff': diff, 'conflict': revision(current) != latest,
                    'revision': revision(current), 'deleted': current is None}

    def resolve_change(self, path: str, expected: str | None, restore: bool) -> dict:
        with self.lock, self._writer():
            target = self.resolve(path)
            current = self._bytes(target)
            conn = self._journal()
            try:
                row = conn.execute('SELECT original, latest, mode, trash, trash_mode FROM changes WHERE path=?', (self.relative(target),)).fetchone()
                if row is None:
                    raise WorkspaceError('没有此文件的工作区变更记录。', 404)
                if revision(current) != expected or restore and revision(current) != row[1]:
                    raise WorkspaceError('文件已变化，请刷新并比较后处理；未覆盖磁盘内容。', 409)
                recovered = restore and current is None and row[1] is None and row[3] is not None
                destination = row[3] if recovered else row[0]
                if restore and current != destination:
                    self._replace(target, destination, row[4] if recovered else row[2])
                if recovered and destination != row[0]:
                    conn.execute('UPDATE changes SET latest=?, trash=NULL, trash_mode=NULL WHERE path=?',
                                 (revision(destination), self.relative(target)))
                else:
                    conn.execute('DELETE FROM changes WHERE path=?', (self.relative(target),))
                conn.commit()
            finally:
                conn.close()
            return {'path': self.relative(target), 'restored': restore, 'recovered': recovered}


@lru_cache(maxsize=8)
def _workspace(root: str) -> Workspace:
    return Workspace(root)


def get_workspace() -> Workspace:
    return _workspace(str(Path(os.getenv('AGENT_EXEC_CWD') or os.getcwd()).resolve()))
