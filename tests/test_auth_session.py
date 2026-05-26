"""Lane D — Admin dashboard cookie-session + login rate-limit tests.

Hit the FastAPI app via TestClient so we exercise the full HTTP stack: cookie
attributes (HttpOnly / SameSite / Secure), the X-Requested-With CSRF gate, the
sliding-window lockout, and the session lifecycle (validate → expire → revoke).
Six tests, matching the design doc §"测试与验收" list:

    test_login_success
    test_login_wrong_key
    test_login_lockout
    test_session_validate
    test_session_expired
    test_logout
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import API_KEY, load_server, make_workspace


CSRF_HEADERS = {'X-Requested-With': 'XMLHttpRequest'}


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """Fresh server module per test — sqlite session DB lives under tmp_path
    and lockout counters start at zero so order-of-tests doesn't matter."""
    workspace = make_workspace(tmp_path)
    monkeypatch.setenv('TM_ENV', 'dev')  # disable Secure flag so TestClient sees cookies
    server = load_server(workspace, monkeypatch)
    server._reset_ui_singletons()
    return TestClient(server.app)


def test_login_success(client: TestClient) -> None:
    """Correct key → 200 + Set-Cookie with hardened attributes."""
    resp = client.post(
        '/admin/ui/login',
        json={'api_key': API_KEY},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 'api_key_hash' in body and len(body['api_key_hash']) == 12
    assert body['expires_at'] > int(time.time())
    assert body['env'] == 'dev'

    # Cookie attributes — TestClient surfaces them via response.cookies + the
    # raw Set-Cookie header. We check both: jar (so the cookie is sent on
    # follow-up requests) and header text (so the hardening flags are right).
    assert 'tm_sid' in resp.cookies
    set_cookie = resp.headers.get('set-cookie', '')
    assert 'tm_sid=' in set_cookie
    assert 'HttpOnly' in set_cookie
    assert 'SameSite=strict' in set_cookie.lower() or 'samesite=strict' in set_cookie.lower()


def test_login_wrong_key(client: TestClient) -> None:
    """Wrong key → 401; no cookie issued."""
    resp = client.post(
        '/admin/ui/login',
        json={'api_key': 'totally-not-the-key'},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 401
    assert 'tm_sid' not in resp.cookies


def test_login_lockout(client: TestClient) -> None:
    """Five failed attempts inside the window → the sixth request is 429 even
    if the key is then suddenly correct. Demonstrates per-IP throttling and
    proves the constant-time check doesn't leak around the rate limit."""
    for _ in range(5):
        bad = client.post(
            '/admin/ui/login',
            json={'api_key': 'bad'},
            headers=CSRF_HEADERS,
        )
        assert bad.status_code == 401

    # Sixth attempt — even with the *real* key — should be locked out.
    locked = client.post(
        '/admin/ui/login',
        json={'api_key': API_KEY},
        headers=CSRF_HEADERS,
    )
    assert locked.status_code == 429
    assert 'tm_sid' not in locked.cookies


def test_session_validate(client: TestClient) -> None:
    """Cookie-authenticated GET /admin/ui/me returns the hash + expiry."""
    login = client.post(
        '/admin/ui/login',
        json={'api_key': API_KEY},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    token = login.cookies.get('tm_sid')
    assert token

    me = client.get('/admin/ui/me', cookies={'tm_sid': token})
    assert me.status_code == 200
    body = me.json()
    assert body['api_key_hash'] == login.json()['api_key_hash']
    assert body['expires_at'] == login.json()['expires_at']


def test_session_expired(tmp_path: Path, monkeypatch) -> None:
    """A session whose ``expires_at`` has lapsed → 401 on /admin/ui/me; the
    row is also GC'd inline so it can't be replayed later."""
    workspace = make_workspace(tmp_path)
    monkeypatch.setenv('TM_ENV', 'dev')
    monkeypatch.setenv('TM_UI_SESSION_TTL', '60')  # any positive value; we'll override the row
    server = load_server(workspace, monkeypatch)
    server._reset_ui_singletons()
    client = TestClient(server.app)

    resp = client.post(
        '/admin/ui/login',
        json={'api_key': API_KEY},
        headers=CSRF_HEADERS,
    )
    token = resp.cookies.get('tm_sid')
    assert token

    # Manually backdate the row so the cookie is technically expired now.
    store = server.get_ui_session_store()
    with store._conn() as conn:
        conn.execute(
            'UPDATE ui_sessions SET expires_at = ? WHERE token = ?',
            (int(time.time()) - 1, token),
        )

    me = client.get('/admin/ui/me', cookies={'tm_sid': token})
    assert me.status_code == 401

    # Row should be deleted by validate() inline GC — second call still 401
    # and the row is gone.
    me_again = client.get('/admin/ui/me', cookies={'tm_sid': token})
    assert me_again.status_code == 401
    assert store.validate(token) is None


def test_logout(client: TestClient) -> None:
    """POST /admin/ui/logout revokes the server-side row + clears the cookie."""
    login = client.post(
        '/admin/ui/login',
        json={'api_key': API_KEY},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    token = login.cookies.get('tm_sid')

    out = client.post(
        '/admin/ui/logout',
        cookies={'tm_sid': token},
        headers=CSRF_HEADERS,
    )
    assert out.status_code == 200
    # Cookie cleared on the browser side via Set-Cookie max-age=0 / expires
    assert 'tm_sid' in out.headers.get('set-cookie', '').lower()

    # Server-side row gone — subsequent /me with the same token → 401.
    me = client.get('/admin/ui/me', cookies={'tm_sid': token})
    assert me.status_code == 401


def test_login_missing_csrf_header(client: TestClient) -> None:
    """Bonus guardrail — POST without X-Requested-With is refused (400)."""
    resp = client.post('/admin/ui/login', json={'api_key': API_KEY})
    assert resp.status_code == 400
