"""Real temporary filesystem/API tests; no models, user documents or SDK."""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from src.agent.workspace import Workspace, WorkspaceError, MAX_BYTES
from src.agent.service import _read_file, _write_file, _edit_file, _delete_file, _search_content
from src.agent.workflow import route_request


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_EXEC_CWD', str(tmp_path))
    monkeypatch.setenv('AGENT_ENABLE_WORKSPACE', '1')
    return Workspace(tmp_path)


def test_roundtrip_conflict_delete_restart_restore(ws):
    ws.write('notes/中文.md', '第一版\r\n', None)
    first = ws.read('notes/中文.md')
    ws.write(first['path'], '第二版\n', first['revision'])
    with pytest.raises(WorkspaceError, match='已被修改'):
        ws.write(first['path'], '过期草稿', first['revision'])
    item = ws.read(first['path'])
    ws.delete(item['path'], item['revision'])
    restarted = Workspace(ws.root)
    assert restarted.changes()[0]['status'] == 'deleted'
    assert '-第二版' in restarted.diff(item['path'])['diff']
    restarted.resolve_change(item['path'], None, True)
    # Recover the latest pre-delete bytes, even for a newly created file.
    assert restarted.read(item['path'])['content'] == '第二版\n'
    assert restarted.changes()[0]['status'] == 'added'
    # A separate revert of the accumulated addition returns to the baseline.
    restarted.resolve_change(item['path'], item['revision'], True)
    assert not (ws.root / item['path']).exists()
    assert restarted.changes() == []


def test_existing_file_backup_preserves_bytes_and_mode(ws):
    target = ws.root / 'old.txt'
    target.write_bytes(b'old\r\n')
    mode = target.stat().st_mode
    old = ws.read('old.txt')
    ws.write('old.txt', 'new\n', old['revision'])
    current = ws.read('old.txt')
    ws.delete('old.txt', current['revision'])
    assert Workspace(ws.root).diff('old.txt')['deleted'] is True
    ws.resolve_change('old.txt', None, True)
    assert target.read_bytes() == b'new\n'
    ws.resolve_change('old.txt', ws.read('old.txt')['revision'], True)
    assert target.read_bytes() == b'old\r\n'
    # Existing writes preserve file permissions.
    assert target.stat().st_mode == mode


@pytest.mark.parametrize('path', ['../escape.txt', '.git/config', '.clerk/data/tasks.csv', '.env',
    'nested/.env.local', 'nested/private.key', '.mokoa-workspace/changes.sqlite3', 'file.txt:stream',
    'CON.txt', 'file.txt.', 'file.txt ', '.ssh/id_rsa'])
def test_protected_paths_cannot_read_write_or_delete(ws, path):
    with pytest.raises(WorkspaceError):
        ws.resolve(path)
    with pytest.raises(WorkspaceError):
        ws.write(path, 'bad', None)


def test_hardlink_and_directory_link_are_not_followed(ws, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (outside / 'secret.txt').write_text('private', encoding='utf-8')
    os.link(outside / 'secret.txt', ws.root / 'hard.txt')
    with pytest.raises(WorkspaceError, match='硬链接'):
        ws.read('hard.txt')
    link = ws.root / 'link'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match='链接|联接'):
        ws.write('link/secret.txt', 'bad', None)
    assert 'link' not in [e['name'] for e in ws.listing()['entries']]
    assert (outside / 'secret.txt').read_text() == 'private'


def test_binary_large_and_utf8_limits(ws):
    (ws.root / 'binary.bin').write_bytes(b'\x00abc')
    (ws.root / 'latin.txt').write_bytes(b'\xff')
    (ws.root / 'large.txt').write_bytes(b'a' * (MAX_BYTES+1))
    for path in ['binary.bin', 'latin.txt', 'large.txt']:
        with pytest.raises(WorkspaceError):
            ws.read(path)
    with pytest.raises(WorkspaceError):
        ws.write('unicode.txt', '中' * (MAX_BYTES//3+1), None)
    ws.write('empty.txt', '', None)
    assert ws.read('empty.txt')['bytes'] == 0


def test_directory_paging_and_secret_filter(ws):
    for name in ['b.txt', 'a.txt', '.env', '.env.example']:
        (ws.root / name).write_text('data', encoding='utf-8')
    (ws.root / 'folder').mkdir()
    one = ws.listing(limit=2)
    two = ws.listing(offset=one['next_offset'], limit=2)
    assert one['entries'][0]['kind'] == 'directory'
    names = [e['name'] for e in one['entries'] + two['entries']]
    assert '.env' not in names and '.env.example' in names
    assert two['next_offset'] is None


def test_deleted_empty_new_file_recovers_and_eof_only_diff_is_visible(ws):
    ws.write('empty.txt', '', None)
    ws.delete('empty.txt', ws.read('empty.txt')['revision'])
    assert Workspace(ws.root).resolve_change('empty.txt', None, True)['recovered']
    assert ws.read('empty.txt')['content'] == ''
    (ws.root / 'eof.txt').write_bytes(b'line')
    ws.write('eof.txt', 'line\n', ws.read('eof.txt')['revision'])
    diff = ws.diff('eof.txt')['diff']
    assert '-line\n\\ No newline at end of file\n+line' in diff


def test_journal_limit_refuses_new_changes_without_losing_existing_recovery(ws, monkeypatch):
    monkeypatch.setattr('src.agent.workspace.MAX_CHANGES', 1)
    ws.write('first.txt', 'one', None)
    with pytest.raises(WorkspaceError, match='上限'):
        ws.write('second.txt', 'two', None)
    assert not (ws.root / 'second.txt').exists()
    ws.write('first.txt', 'updated', ws.read('first.txt')['revision'])
    assert len(ws.changes()) == 1


def test_external_changes_block_restore_accept_requires_current_revision(ws):
    target = ws.root / 'a.txt'
    target.write_text('original')
    ws.write('a.txt', 'saved', ws.read('a.txt')['revision'])
    saved = ws.read('a.txt')['revision']
    target.write_text('teammate')
    assert ws.changes()[0]['conflict']
    assert ws.diff('a.txt')['conflict']
    with pytest.raises(WorkspaceError, match='已变化'):
        ws.resolve_change('a.txt', saved, True)
    current = ws.read('a.txt')['revision']
    with pytest.raises(WorkspaceError):
        ws.resolve_change('a.txt', current, True)
    ws.resolve_change('a.txt', current, False)
    assert target.read_text() == 'teammate' and not ws.changes()


def test_two_concurrent_saves_only_one_wins(ws):
    ws.write('race.txt', 'v1', None)
    old = ws.read('race.txt')['revision']
    def save(text):
        try:
            # Separate service instances exercise the cross-worker writer lock.
            Workspace(ws.root).write('race.txt', text, old)
            return 200
        except WorkspaceError as e:
            return e.status
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, ['v2', 'v3'])) == [200, 409]


def test_agent_tools_share_journal_and_hide_secrets(ws):
    (ws.root / '.env').write_text('SECRET_TOKEN=not-real')
    assert not _read_file('.env')[0]
    assert _write_file('agent.txt', 'a\nb\n')[0]
    assert _edit_file('agent.txt', 'b', 'c')[0]
    result = _search_content('SECRET_TOKEN')[1]
    assert '.env' not in result and 'not-real' not in result
    assert ws.changes()[0]['path'] == 'agent.txt'
    assert _delete_file('agent.txt')[0]
    assert ws.changes()[0]['status'] == 'deleted'


@pytest.mark.parametrize('message', ['删除 guide.txt', '请删除文件 notes/a.md', '删除目录 notes', '帮我删掉中文文件'])
def test_file_deletion_never_routes_to_memory_forgetting(message):
    assert route_request(message) == 'tool_task'


def test_api_optin_revision_errors_and_recovery(ws, monkeypatch):
    from api.server import app
    with TestClient(app) as client:
        monkeypatch.setenv('AGENT_ENABLE_WORKSPACE', '0')
        assert client.get('/workspace').json()['enabled'] is False
        assert client.get('/workspace/tree').status_code == 403
        monkeypatch.setenv('AGENT_ENABLE_WORKSPACE', '1')
        assert client.get('/workspace/tree').status_code == 200
        assert client.put('/workspace/file', json={'path': 'api.txt', 'content': 'x'}).status_code == 422
        payload = {'path': 'api.txt', 'content': 'x', 'revision': None}
        assert client.put('/workspace/file', json=payload).status_code == 200
        assert client.put('/workspace/file', json=payload).status_code == 409
        item = client.get('/workspace/file', params={'path': 'api.txt'}).json()
        assert client.request('DELETE', '/workspace/file', json={'path': 'api.txt', 'revision': item['revision']}).status_code == 200
        assert client.get('/workspace/file', params={'path': 'api.txt'}).status_code == 404
        assert client.get('/workspace/diff', params={'path': 'api.txt'}).json()['deleted']
        assert client.post('/workspace/restore', json={'path': 'api.txt', 'revision': None}).status_code == 200
        assert client.get('/workspace/file', params={'path': '../bad'}).status_code == 403
