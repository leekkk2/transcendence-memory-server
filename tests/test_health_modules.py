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


# ────────────────── Profiles 相关：/admin/profiles + /admin/probe-embedding ──────────────────
#
# 这些测试 stub 出 EmbeddingRegistry 单例，避免依赖真实 profiles.yaml / 网络。
# 关键安全契约：api_key 真值禁止落入 response；公开 /health 禁止暴露 profile 名 / 计数。


def _install_fake_registry(server, monkeypatch):
    """注入一个最小可用的 fake registry，覆盖 embeddings/rerankers/routes/default_route。

    pyproject 的 pythonpath = ['src', 'scripts'] 让 `scripts.embedding_registry` 与
    顶层 `embedding_registry` 同时被识别为两个独立模块对象，各自持有 `_registry` 单例。
    Server 端点先 try 顶层、再 fall back 到 `scripts.*`，所以两份都要打补丁，否则测
    试只 patch 其中一份会落到另一份的 legacy fallback。
    """
    from scripts.profiles_loader import EmbeddingProfile, RerankerProfile, Route, ProfileSet
    from scripts import embedding_registry as er
    import embedding_registry as er_top  # pyproject pythonpath 让这条 import 生效

    emb_primary = EmbeddingProfile(
        name='gemini-3072', provider='openai_compatible',
        model='gemini-embedding-001', dim=3072,
        base_url='https://newapi.example/v1', api_key='SECRET-DO-NOT-LEAK',
        max_token_size=8192, request_dim=None, timeout_s=60.0, max_retries=3,
    )
    emb_fb = EmbeddingProfile(
        name='openai-3072', provider='openai_compatible',
        model='text-embedding-3-large', dim=3072,
        base_url='https://api.openai.com/v1', api_key='ANOTHER-SECRET',
        max_token_size=8192, request_dim=3072, timeout_s=60.0, max_retries=3,
    )
    rrk = RerankerProfile(
        name='cohere-rerank', provider='cohere_compatible',
        model='rerank-multilingual-v3.0',
        base_url='https://api.cohere.com/v1', api_key='RRK-SECRET',
        timeout_s=30.0, min_score=0.2,
    )
    route_imac = Route(embedding='gemini-3072', embedding_fallbacks=('openai-3072',))
    default_route = Route(embedding='gemini-3072')
    ps = ProfileSet(
        embeddings={'gemini-3072': emb_primary, 'openai-3072': emb_fb},
        rerankers={'cohere-rerank': rrk},
        routes=[({'exact': 'imac'}, route_imac)],
        default_route=default_route,
    )
    fake = er.EmbeddingRegistry(ps)
    monkeypatch.setattr(er, '_registry', fake)
    monkeypatch.setattr(er_top, '_registry', fake)
    return ps


def test_admin_profiles_requires_auth(tmp_path, monkeypatch):
    """/admin/profiles 无 key 应被拒。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/profiles')
    assert resp.status_code in (401, 403, 422), f'unexpected status: {resp.status_code}'


def test_admin_profiles_returns_full_detail(tmp_path, monkeypatch):
    """带 key 时应返回 embeddings + rerankers + routes + default_route 全部字段。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/profiles', headers=auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert 'embeddings' in data
    assert 'rerankers' in data
    assert 'routes' in data
    assert 'default_route' in data
    # embeddings/rerankers 都至少有 1 条
    names = {e['name'] for e in data['embeddings']}
    assert {'gemini-3072', 'openai-3072'}.issubset(names)
    rrk_names = {r['name'] for r in data['rerankers']}
    assert 'cohere-rerank' in rrk_names
    # routes 至少 1 条、含 match dict
    assert len(data['routes']) == 1
    assert data['routes'][0]['match'] == {'exact': 'imac'}
    assert data['routes'][0]['embedding'] == 'gemini-3072'
    assert data['routes'][0]['embedding_fallbacks'] == ['openai-3072']
    # default_route 含 embedding 字段
    assert data['default_route']['embedding'] == 'gemini-3072'


def test_admin_profiles_reranker_fields_complete(tmp_path, monkeypatch):
    """v0.8.0：/admin/profiles 返回的 reranker 项必含 model/provider/base_url/
    timeout_s/min_score 字段，并保留 api_key_configured 安全契约。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/profiles', headers=auth_headers())
    data = resp.json()
    assert data['rerankers'], 'expected at least one reranker entry'
    r = data['rerankers'][0]
    # v0.8.0 必含字段集 — 客户端可据此渲染 reranker 列表 UI
    for field in ('name', 'provider', 'model', 'base_url', 'timeout_s',
                  'min_score', 'api_key_configured'):
        assert field in r, f'rerankers[0] missing field {field!r}: {r}'
    # api_key 真值不可出现
    assert 'api_key' not in r


def test_admin_profiles_route_reranker_fields_present(tmp_path, monkeypatch):
    """v0.8.0：route 视图必带 reranker / rerank_enabled / chunk_top_k / top_k
    字段，让客户端能预知服务端 rerank 策略。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/profiles', headers=auth_headers())
    data = resp.json()
    # default_route 也带这些字段（dataclass 默认值兜底，schema 必稳）
    dr = data['default_route']
    for field in ('reranker', 'rerank_enabled', 'chunk_top_k', 'top_k'):
        assert field in dr, f'default_route missing field {field!r}: {dr}'
    # routes[0] 同样字段集
    if data['routes']:
        for field in ('reranker', 'rerank_enabled', 'chunk_top_k', 'top_k'):
            assert field in data['routes'][0], f'routes[0] missing field {field!r}'


def test_admin_profiles_redacts_api_key(tmp_path, monkeypatch):
    """response 必须**不含** api_key 真值字段，但**应该**含 api_key_configured: bool。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/profiles', headers=auth_headers())
    data = resp.json()
    # 字段级：每条 embedding/reranker 不含 api_key，但有 api_key_configured
    for e in data['embeddings']:
        assert 'api_key' not in e, f'embedding leaks api_key field: {e}'
        assert 'api_key_configured' in e and isinstance(e['api_key_configured'], bool)
    for r in data['rerankers']:
        assert 'api_key' not in r, f'reranker leaks api_key field: {r}'
        assert 'api_key_configured' in r and isinstance(r['api_key_configured'], bool)
    # 文本级：fake registry 注入的几个 SECRET 字符串不应出现在序列化结果中
    raw = resp.text
    for needle in ('SECRET-DO-NOT-LEAK', 'ANOTHER-SECRET', 'RRK-SECRET'):
        assert needle not in raw, f'api_key value leaked in response body: {needle!r}'


def test_admin_probe_embedding_unknown_profile(tmp_path, monkeypatch):
    """未知 profile name → 404，避免吞掉客户端配置错误。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.post('/admin/probe-embedding?profile=nonexistent', headers=auth_headers())
    assert resp.status_code == 404
    assert 'nonexistent' in resp.json()['detail']


def test_admin_probe_embedding_requires_auth(tmp_path, monkeypatch):
    """/admin/probe-embedding 无 key 应被拒。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.post('/admin/probe-embedding?profile=anything')
    assert resp.status_code in (401, 403, 422), f'unexpected status: {resp.status_code}'


def test_admin_probe_embedding_resets_breaker_on_success(tmp_path, monkeypatch):
    """v0.9.0：探活成功 → 显式重置该 profile 的 circuit breaker，
    response 含 breaker_reset 字段。

    先人为把 breaker 推到 open 状态，再 probe 成功 → 应 reset。
    """
    import numpy as np
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)

    # 同时打两份模块（与 _install_fake_registry 一样的双 import 处理）—
    # pyproject pythonpath = ['src', 'scripts'] 让顶层 / scripts.* 是两个独立模块
    from scripts import embedding_registry as er
    import embedding_registry as er_top

    # 让 _http_embed 不发真 HTTP：返回固定 dim=3072 的数组
    async def fake_embed(profile, texts):
        return np.zeros((len(texts), 3072), dtype="float32")

    monkeypatch.setattr(er, "_http_embed", fake_embed)
    monkeypatch.setattr(er_top, "_http_embed", fake_embed)

    # 人为构造一个 open 状态（直接写 breaker dict，等价于实际跑过 5 次失败）
    # 注意：scripts.embedding_registry 和顶层 embedding_registry 是两个独立
    # 模块对象（pyproject pythonpath = ['src', 'scripts'] 双注册），各自
    # 持有独立的 _breakers dict。Server 端点先 try 顶层，所以 breaker 写
    # 的也是顶层那份；两边都要 setup 才能保证测试不依赖 import 顺序。
    er._clear_all_breakers()
    er_top._clear_all_breakers()
    for mod in (er, er_top):
        state = mod._get_breaker("gemini-3072")
        state.consecutive_fails = 5
        state.cooling_until_ts = 999999999.0
        state.last_fail_ts = 1.0

    client = TestClient(server.app)
    resp = client.post('/admin/probe-embedding?profile=gemini-3072', headers=auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['ok'] is True
    assert data['profile'] == 'gemini-3072'
    assert data['dim'] == 3072
    assert data['breaker_reset'] is True, f"expected breaker_reset=True, got {data}"

    # 端点实际走的那份模块的 state 已清空（用 server 端点同款 import 顺序定位）
    actual_mod = er_top  # endpoint 先 try 顶层 import
    state_after = actual_mod._breakers.get('gemini-3072')
    assert state_after is not None
    assert state_after.consecutive_fails == 0
    assert state_after.cooling_until_ts == 0.0


def test_admin_probe_embedding_failure_marks_breaker_no_reset(tmp_path, monkeypatch):
    """v0.9.0：探活失败（fallback-eligible 错误）→ breaker_reset=False，
    且 breaker 计数加 1（不重置）。"""
    import httpx
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)

    from scripts import embedding_registry as er
    import embedding_registry as er_top

    # 模拟 503 错误（fallback-eligible）
    async def fail_embed(profile, texts):
        request = httpx.Request("POST", f"{profile.base_url}/embeddings")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("upstream 503", request=request, response=response)

    monkeypatch.setattr(er, "_http_embed", fail_embed)
    monkeypatch.setattr(er_top, "_http_embed", fail_embed)

    er._clear_all_breakers()
    er_top._clear_all_breakers()

    client = TestClient(server.app)
    resp = client.post('/admin/probe-embedding?profile=gemini-3072', headers=auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['ok'] is False
    assert data['breaker_reset'] is False
    assert 'error' in data

    # breaker 计数 +1（503 是 fallback-eligible 错误）
    # endpoint 先 try 顶层 embedding_registry → 失败计数写到 er_top._breakers
    actual_mod = er_top
    state = actual_mod._breakers.get('gemini-3072')
    assert state is not None, "probe failure 应触发 breaker mark"
    assert state.consecutive_fails == 1


def test_admin_system_health_includes_profile_summary(tmp_path, monkeypatch):
    """/admin/system-health 应包含 profiles.embeddings_count / rerankers_count。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/admin/system-health', headers=auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert 'profiles' in data, 'admin response missing profiles summary'
    profiles = data['profiles']
    assert profiles is not None
    assert profiles['embeddings_count'] == 2
    assert profiles['rerankers_count'] == 1
    assert profiles['default_route_embedding'] == 'gemini-3072'


def test_public_health_does_not_leak_profile_names(tmp_path, monkeypatch):
    """公开 /health 不应含 'profiles' / 'embedding_model' 等多模型相关字段。

    向后保护 user 之前对 /health 做的安全收口 — profiles summary 是 admin-only。
    """
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    _install_fake_registry(server, monkeypatch)
    client = TestClient(server.app)
    resp = client.get('/health')
    data = resp.json()
    forbidden = ('profiles', 'embedding_model', 'embeddings_count', 'default_route_embedding')
    for f in forbidden:
        assert f not in data, f'/health leaks multi-model field: {f}'
    # 字符串级：profile 名也不应出现在 /health body
    raw = resp.text
    for needle in ('gemini-3072', 'openai-3072', 'cohere-rerank'):
        assert needle not in raw, f'/health leaks profile name: {needle!r}'
