"""健康端点测试 — 公开 /health 字段集 + 鉴权 /admin/system-health 详细字段。

设计契约：
- /health 是 LB-style 公开端点，**禁止**泄露容器名、绝对路径、阈值数值、env key 名
  等可被匿名访问者用作侦察的指纹信息。
- /admin/system-health 是鉴权端点，返回 /health 的全集 + 全部敏感诊断字段。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import API_KEY, auth_headers, load_server, make_workspace


# ───────────────────────── 公开 /health ─────────────────────────


def test_health_minimum_public_fields(tmp_path, monkeypatch):
    """/health 必含的最小公开字段集（LB / 客户端依赖）。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    assert resp.status_code == 200
    data = resp.json()
    required = [
        'status', 'service', 'architecture', 'build_flavor', 'multimodal_capable',
        'degraded_reasons', 'runtime_ready', 'accepting_ingest', 'worker_running',
        'uptime_seconds', 'system_status', 'warnings',
    ]
    for f in required:
        assert f in data, f'Missing public health field: {f}'


def test_health_does_not_leak_sensitive_fields(tmp_path, monkeypatch):
    """/health 必须不暴露敏感字段（容器名、路径、env key、阈值数值、队列计数）。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    forbidden = [
        'available_containers',   # 容器/租户名清单
        'configuration_guide',    # 已配置/缺失的 env key 名
        'modules',                # required_keys / missing_keys
        'scripts_present',        # 内部文件存在性
        'workspace',              # 绝对路径
        'containers_root',        # 绝对路径
        'system',                 # 原始 cgroup / load 数值
        'thresholds',             # 503 触发阈值精确值
        'queue_stats',            # 队列计数
        'background_jobs_active', # 后台 job 计数
        'auth_configured',        # 鉴权配置状态指纹
        'embedding_configured',
        'lancedb_available',
    ]
    for f in forbidden:
        assert f not in data, f'/health leaks sensitive field: {f}'


def test_health_system_status_uses_labels_not_numbers(tmp_path, monkeypatch):
    """system_status 应是 'ok' / 'pressure' 标签，不含数值。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert isinstance(data['system_status'], dict)
    for dim, label in data['system_status'].items():
        assert label in ('ok', 'pressure'), f'system_status[{dim}] leaked value: {label}'


def test_health_warnings_redacted(tmp_path, monkeypatch):
    """公开 warnings 不应含 'MB' / 'threshold N' / 数字阈值。"""
    workspace = make_workspace(tmp_path)
    monkeypatch.delenv('RAG_API_KEY', raising=False)  # 触发 'RAG_API_KEY not configured' 类警告
    server = load_server(workspace, monkeypatch)
    # 把 conftest 默认 RAG_API_KEY 设置清掉
    server.RAG_API_KEY = ''
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    for w in data['warnings']:
        assert 'MB' not in w, f'public warning leaks memory value: {w}'
        assert 'threshold' not in w.lower(), f'public warning leaks threshold: {w}'
        assert 'RAG_API_KEY' not in w, f'public warning leaks env name: {w}'


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


def test_runtime_ready_includes_query(tmp_path, monkeypatch):
    """runtime_ready 应包含 query / documents_text — 客户端会读这两个。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert 'query' in data['runtime_ready']
    assert 'documents_text' in data['runtime_ready']


def test_health_returns_build_fields(tmp_path, monkeypatch):
    """build_flavor / multimodal_capable / degraded_reasons 仍公开（无数值，弱指纹）。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch, {'TM_BUILD_FLAVOR': 'lite'})
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert data['build_flavor'] == 'lite'
    assert 'multimodal_capable' in data
    assert 'degraded_reasons' in data


def test_health_warns_when_vlm_used_in_lite_build(tmp_path, monkeypatch):
    """lite 构建下启用 VLM 时降级原因仍可见。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch, {
        'TM_BUILD_FLAVOR': 'lite',
        'EMBEDDING_API_KEY': 'test-key',
        'LLM_API_KEY': 'test-llm-key',
        'VLM_API_KEY': 'test-vlm-key',
    })
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    assert data['build_flavor'] == 'lite'
    assert any('lite build' in reason for reason in data['degraded_reasons'])
    assert any('lite build' in warning for warning in data['warnings'])


# ────────────────── 鉴权 /admin/system-health ──────────────────


def test_admin_system_health_requires_auth(tmp_path, monkeypatch):
    """/admin/system-health 无 key 应被拒。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/system-health')
    assert resp.status_code in (401, 403, 422), f'unexpected status: {resp.status_code}'


def test_admin_system_health_returns_full_detail(tmp_path, monkeypatch):
    """带 key 时应返回 /health 的超集 + 全部敏感字段。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/system-health', headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    # 必有公开层字段
    for f in ('status', 'service', 'architecture', 'runtime_ready', 'accepting_ingest', 'system_status'):
        assert f in data, f'admin missing public field: {f}'
    # 必有敏感诊断字段（搬过来的）
    for f in ('available_containers', 'configuration_guide', 'modules',
              'scripts_present', 'workspace', 'containers_root',
              'system', 'thresholds', 'queue_stats', 'background_jobs_active',
              'admit_ok', 'admit_reason', 'gate_config',
              'background_jobs', 'background_max_alive', 'retry_cooldown_sec'):
        assert f in data, f'admin missing sensitive field: {f}'


def test_admin_thresholds_reflect_env_override(tmp_path, monkeypatch):
    """env 改阈值后 /admin/system-health 立刻反映。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch, {
        'TM_MIN_AVAILABLE_MEM_MB': '1500',
        'TM_MAX_LOAD_PER_CPU': '6.0',
    })
    client = TestClient(server.app)
    resp = client.get('/admin/system-health', headers=auth_headers())
    data = resp.json()
    assert data['thresholds']['min_available_mem_mb'] == 1500
    assert data['thresholds']['max_load_per_cpu'] == 6.0
    # gate_config 是兼容字段
    assert data['gate_config']['min_available_mem_mb'] == 1500


def test_admin_modules_contain_required_keys(tmp_path, monkeypatch):
    """admin 端点 modules 字段需含 required_keys / missing_keys 供运维诊断。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/system-health', headers=auth_headers())
    data = resp.json()
    for mod_data in data['modules'].values():
        assert 'required_keys' in mod_data
        assert 'missing_keys' in mod_data


def test_admin_configuration_guide_lists_keys(tmp_path, monkeypatch):
    """admin 端点 configuration_guide 应列出 configured / missing / optional env keys。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/system-health', headers=auth_headers())
    data = resp.json()
    guide = data['configuration_guide']
    assert 'configured' in guide
    assert 'missing' in guide
    assert 'optional' in guide
    assert 'RAG_API_KEY' in guide['configured']
