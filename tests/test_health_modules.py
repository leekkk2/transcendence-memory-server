"""健康端点扩展测试 — 验证 modules 和 configuration_guide 字段。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import load_server, make_workspace, API_KEY


def test_health_returns_modules(tmp_path, monkeypatch):
    """health 端点应返回 modules 字段。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    assert resp.status_code == 200
    data = resp.json()
    assert 'modules' in data
    assert 'lancedb' in data['modules']
    assert 'lightrag' in data['modules']
    assert 'multimodal' in data['modules']
    for mod_data in data['modules'].values():
        assert 'enabled' in mod_data
        assert 'ready' in mod_data
        assert 'package_available' in mod_data
        assert 'required_keys' in mod_data
        assert 'missing_keys' in mod_data


def test_health_returns_configuration_guide(tmp_path, monkeypatch):
    """health 端点应返回 configuration_guide 字段。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert 'configuration_guide' in data
    guide = data['configuration_guide']
    assert 'configured' in guide
    assert 'missing' in guide
    assert 'optional' in guide
    # RAG_API_KEY 在 conftest 中设了
    assert 'RAG_API_KEY' in guide['configured']


def test_health_architecture_dynamic(tmp_path, monkeypatch):
    """无 LLM key 时 architecture 应为 lancedb-only。"""
    workspace = make_workspace(tmp_path)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('VLM_API_KEY', raising=False)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert data['architecture'] == 'lancedb-only'


def test_health_backward_compatible(tmp_path, monkeypatch):
    """所有原有字段仍存在。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    required_fields = [
        'status', 'service', 'architecture', 'workspace', 'containers_root',
        'auth_configured', 'embedding_configured', 'lancedb_available',
        'scripts_present', 'runtime_ready', 'available_containers', 'warnings', 'uptime_seconds',
    ]
    for field_name in required_fields:
        assert field_name in data, f'Missing backward-compatible field: {field_name}'


def test_runtime_ready_includes_query(tmp_path, monkeypatch):
    """runtime_ready 应包含 query 和 documents_text 字段。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert 'query' in data['runtime_ready']
    assert 'documents_text' in data['runtime_ready']
