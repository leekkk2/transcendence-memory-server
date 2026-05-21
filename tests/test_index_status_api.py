"""Tests for the container index-state machine + embedding backlog API.

覆盖：
- 各状态（fresh / backlog / quota_blocked / error）下 /containers/{name}/index-status
  返回正确 state；
- /index-status 全容器批量；
- /containers/{name}/backlog 明细 + status 过滤；
- 鉴权（无 key → 401）；
- /containers 列表项含 index_state 字段。

backlog / index_state 与 JobQueue 共用 WS/tasks/rag/queue.db；测试直接用 server
暴露的 get_backlog_store() / get_index_state_store() 单例预置状态，再打端点验证。
"""
from __future__ import annotations

from pathlib import Path

from conftest import API_KEY, auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


def _setup(tmp_path: Path, monkeypatch):
    """加载 server，返回 (server_module, TestClient)。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    return server, TestClient(server.app)


# ───────────────────────── 单容器状态机 ─────────────────────────


def test_index_status_fresh(tmp_path, monkeypatch):
    """全部对象已嵌、backlog 空 → fresh。"""
    server, client = _setup(tmp_path, monkeypatch)
    server.get_index_state_store().record_embed_run(
        'freshbox', total_objects=3, embedded_objects=3, succeeded=True,
    )
    resp = client.get('/containers/freshbox/index-status', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['state'] == 'fresh'
    assert data['container'] == 'freshbox'
    assert data['total_objects'] == 3
    assert data['embedded_objects'] == 3
    assert data['backlog_active'] == 0
    assert data['dead_count'] == 0


def test_index_status_backlog(tmp_path, monkeypatch):
    """有 transient 失败 chunk 在 backlog → backlog。"""
    server, client = _setup(tmp_path, monkeypatch)
    server.get_backlog_store().record_failure('backlogbox', 'chunk-1', 'transient')
    resp = client.get('/containers/backlogbox/index-status', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['state'] == 'backlog'
    assert data['backlog_active'] == 1
    assert data['last_error_class'] == 'transient'
    assert data['next_retry_at'] is not None


def test_index_status_quota_blocked(tmp_path, monkeypatch):
    """配额耗尽失败 chunk → quota_blocked。"""
    server, client = _setup(tmp_path, monkeypatch)
    server.get_backlog_store().record_failure('quotabox', 'chunk-1', 'quota')
    resp = client.get('/containers/quotabox/index-status', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['state'] == 'quota_blocked'
    assert data['last_error_class'] == 'quota'
    assert data['backlog_active'] == 1


def test_index_status_error(tmp_path, monkeypatch):
    """永久失败（dead-letter）→ error。"""
    server, client = _setup(tmp_path, monkeypatch)
    server.get_backlog_store().record_failure('errorbox', 'chunk-1', 'permanent')
    resp = client.get('/containers/errorbox/index-status', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['state'] == 'error'
    assert data['dead_count'] == 1
    assert data['backlog_active'] == 0


def test_index_status_unknown_container_404(tmp_path, monkeypatch):
    """完全无记录、无 backlog、目录不存在 → 404。"""
    _, client = _setup(tmp_path, monkeypatch)
    resp = client.get('/containers/ghostbox/index-status', headers=auth_headers())
    assert resp.status_code == 404


def test_index_status_requires_auth(tmp_path, monkeypatch):
    """无 API key → 401。"""
    _, client = _setup(tmp_path, monkeypatch)
    assert client.get('/containers/anybox/index-status').status_code == 401
    assert client.get('/index-status').status_code == 401
    assert client.get('/containers/anybox/backlog').status_code == 401


# ───────────────────────── 批量状态机 ─────────────────────────


def test_index_status_batch(tmp_path, monkeypatch):
    """/index-status 覆盖 index_state 记录 ∪ backlog 容器 ∪ containers 目录。"""
    server, client = _setup(tmp_path, monkeypatch)
    server.get_index_state_store().record_embed_run(
        'alpha', total_objects=2, embedded_objects=2, succeeded=True,
    )
    server.get_backlog_store().record_failure('beta', 'chunk-1', 'quota')

    resp = client.get('/index-status', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    by_name = {c['container']: c['state'] for c in data['containers']}
    assert by_name.get('alpha') == 'fresh'
    assert by_name.get('beta') == 'quota_blocked'
    assert data['count'] == len(data['containers'])


# ───────────────────────── backlog 明细 ─────────────────────────


def test_container_backlog_lists_items(tmp_path, monkeypatch):
    """/containers/{name}/backlog 返回明细，含 dead 项。"""
    server, client = _setup(tmp_path, monkeypatch)
    store = server.get_backlog_store()
    store.record_failure('mixbox', 'chunk-wait', 'transient')
    store.record_failure('mixbox', 'chunk-dead', 'permanent')

    resp = client.get('/containers/mixbox/backlog', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['container'] == 'mixbox'
    assert data['count'] == 2
    assert data['active'] == 1
    assert data['dead'] == 1
    statuses = {it['chunk_id']: it['status'] for it in data['items']}
    assert statuses['chunk-wait'] == 'waiting'
    assert statuses['chunk-dead'] == 'dead'


def test_container_backlog_status_filter(tmp_path, monkeypatch):
    """status query 参数过滤；非法值 → 400。"""
    server, client = _setup(tmp_path, monkeypatch)
    store = server.get_backlog_store()
    store.record_failure('filterbox', 'chunk-wait', 'transient')
    store.record_failure('filterbox', 'chunk-dead', 'permanent')

    resp = client.get(
        '/containers/filterbox/backlog', params={'status': 'dead'}, headers=auth_headers(),
    )
    assert resp.status_code == 200
    items = resp.json()['items']
    assert len(items) == 1
    assert items[0]['chunk_id'] == 'chunk-dead'

    bad = client.get(
        '/containers/filterbox/backlog', params={'status': 'bogus'}, headers=auth_headers(),
    )
    assert bad.status_code == 400


def test_container_backlog_empty(tmp_path, monkeypatch):
    """无 backlog 的容器返回空列表，不报错。"""
    _, client = _setup(tmp_path, monkeypatch)
    resp = client.get('/containers/cleanbox/backlog', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data['count'] == 0
    assert data['items'] == []


# ───────────────────────── /containers 增强 ─────────────────────────


def test_list_containers_includes_index_state(tmp_path, monkeypatch):
    """/containers 每项含 index_state 字段。"""
    server, client = _setup(tmp_path, monkeypatch)
    containers_root = tmp_path / 'workspace' / 'tasks' / 'rag' / 'containers'
    (containers_root / 'box-a').mkdir(parents=True)
    (containers_root / 'box-b').mkdir(parents=True)
    server.get_backlog_store().record_failure('box-b', 'chunk-1', 'quota')

    resp = client.get('/containers', headers=auth_headers())
    assert resp.status_code == 200
    items = {c['name']: c for c in resp.json()['containers']}
    assert 'index_state' in items['box-a']
    assert 'index_state' in items['box-b']
    assert items['box-b']['index_state'] == 'quota_blocked'


def test_embed_response_mentions_backlog(tmp_path, monkeypatch):
    """/embed 响应 note 说明 embedding 失败对象进 backlog 不丢失。"""
    _, client = _setup(tmp_path, monkeypatch)
    resp = client.post('/embed', json={'container': 'notebox'}, headers=auth_headers())
    assert resp.status_code == 200
    assert 'backlog' in (resp.json().get('note') or '').lower()
