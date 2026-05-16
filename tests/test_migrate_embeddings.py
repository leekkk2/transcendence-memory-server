"""Tests for scripts/migrate_embeddings.py — verify dry-run estimation,
commit re-embedding + atomic rename, refusal to overwrite a staging table,
and embedding metadata updates.

Strategy:
- Use a tmp_path workspace with the exact `tasks/rag/containers/<c>/lancedb`
  layout that task_rag_runtime expects.
- Seed a fake `chunks` table with ~10 rows at dim=4 and old embedding metadata.
- Monkey-patch `migrate_embeddings._build_embed_callable` so we never touch the
  network — the fake callable returns deterministic vectors at the target dim.
- Run main() with stdout/stderr captured via capsys.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import lancedb
import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# scripts/ is on pytest's pythonpath (see pyproject.toml [tool.pytest.ini_options])
import migrate_embeddings as me  # noqa: E402


# --------------------------- fixtures ---------------------------


_FIELDS = (
    "chunkId", "taskId", "docType", "sourcePath", "section", "text",
    "container", "title", "source", "tags", "metadata",
    "embedding_model", "embedding_dim", "embedding_profile",
)


def _make_row(idx: int, dim: int, model: str, profile: str) -> dict:
    """Build a complete row matching task_rag_lancedb_ingest._ROW_SCHEMA_FIELDS."""
    return {
        "chunkId": f"c{idx:03d}",
        "taskId": f"t{idx:03d}",
        "docType": "memory",
        "sourcePath": "fake.md",
        "section": "memory",
        "text": f"hello world chunk {idx} " + ("x" * (10 + idx)),
        "container": "testbox",
        "title": "",
        "source": "",
        "tags": [],
        "metadata": "{}",
        "embedding_model": model,
        "embedding_dim": dim,
        "embedding_profile": profile,
        "vector": [float((idx + j) % 7) * 0.1 for j in range(dim)],
    }


def _seed_table(workspace: Path, container: str, n_rows: int, dim: int,
                model: str = "old-model", profile: str = "from-profile") -> Path:
    """Create the container's chunks table with n_rows fake rows. Returns lancedb path."""
    lancedb_path = workspace / "tasks" / "rag" / "containers" / container / "lancedb"
    lancedb_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lancedb_path))
    rows = [_make_row(i, dim, model, profile) for i in range(n_rows)]
    db.create_table(me.CHUNKS_TABLE, data=rows, mode="create")
    return lancedb_path


def _make_fake_embed(dim: int, model: str):
    """Build a fake embed_callable that returns deterministic vectors at `dim`."""
    def _call(texts):
        return np.array(
            [[float(j + len(t)) * 0.01 for j in range(dim)] for t in texts],
            dtype="float32",
        )
    return _call, dim, model


@pytest.fixture
def stub_registry(monkeypatch):
    """Replace _build_embed_callable + registry.get_profile so tests are network-free.

    Two profiles are exposed:
      - 'profile-from' dim=4 model='old-model'
      - 'profile-to'   dim=8 model='new-model'
    """
    profiles = {
        "profile-from": {"dim": 4, "model": "old-model"},
        "profile-to":   {"dim": 8, "model": "new-model"},
    }

    class _StubProfile:
        def __init__(self, name, info):
            self.name = name
            self.dim = info["dim"]
            self.model = info["model"]

    class _StubRegistry:
        def get_profile(self, name):
            if name not in profiles:
                raise KeyError(f"embedding profile {name!r} not found")
            return _StubProfile(name, profiles[name])

    stub_reg = _StubRegistry()

    def fake_get_registry():
        return stub_reg

    def fake_build_embed_callable(name):
        if name not in profiles:
            raise KeyError(f"embedding profile {name!r} not found")
        info = profiles[name]
        return _make_fake_embed(info["dim"], info["model"])

    monkeypatch.setattr(me, "_get_registry", fake_get_registry)
    monkeypatch.setattr(me, "_build_embed_callable", fake_build_embed_callable)
    return profiles


# --------------------------- dry-run ---------------------------


def test_dry_run_does_not_write_anything(tmp_path, stub_registry, capsys):
    """Dry-run prints summary + leaves table untouched."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=10, dim=4,
                               profile="profile-from")
    # snapshot mtimes to verify no writes
    before_files = sorted(p.name for p in lancedb_path.rglob("*"))

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--batch-size", "3",
        "--workspace", str(workspace),
    ])
    assert rc == 0

    after_files = sorted(p.name for p in lancedb_path.rglob("*"))
    assert before_files == after_files, "dry-run must not write files"

    out = capsys.readouterr().out
    assert "dry-run summary" in out
    assert "total rows" in out and "10" in out
    assert "profile-to" in out
    assert "Re-run with `--commit`" in out


def test_dry_run_includes_token_estimate(tmp_path, stub_registry, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _seed_table(workspace, "testbox", n_rows=5, dim=4, profile="profile-from")

    me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--batch-size", "2",
        "--workspace", str(workspace),
    ])
    out = capsys.readouterr().out
    assert "estimated tokens" in out
    assert "estimated duration" in out


def test_dry_run_empty_table(tmp_path, stub_registry, capsys):
    """Empty source table: report 0 rows, no error."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = workspace / "tasks" / "rag" / "containers" / "testbox" / "lancedb"
    lancedb_path.mkdir(parents=True, exist_ok=True)
    # Need at least one row to bootstrap the schema, then delete it
    db = lancedb.connect(str(lancedb_path))
    db.create_table(me.CHUNKS_TABLE, data=[_make_row(0, 4, "old-model", "profile-from")],
                    mode="create")
    db.open_table(me.CHUNKS_TABLE).delete("chunkId = 'c000'")

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--workspace", str(workspace),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total rows" in out and " 0 " in out


# --------------------------- commit ---------------------------


def test_commit_re_embeds_to_new_dim_and_updates_metadata(tmp_path, stub_registry, capsys):
    """End-to-end commit: dim changes 4 → 8, metadata updated, old table kept."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=10, dim=4,
                               profile="profile-from")

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--batch-size", "3",
        "--commit",
        "--workspace", str(workspace),
    ])
    assert rc == 0

    # 1. chunks table now has new vector dim
    db = lancedb.connect(str(lancedb_path))
    new_table = db.open_table(me.CHUNKS_TABLE)
    assert int(new_table.schema.field("vector").type.list_size) == 8
    assert new_table.count_rows() == 10

    # 2. metadata fields updated to the new profile
    rows = new_table.to_arrow().to_pylist()
    for r in rows:
        assert r["embedding_dim"] == 8
        assert r["embedding_model"] == "new-model"
        assert r["embedding_profile"] == "profile-to"
        # vector length matches dim
        assert len(r["vector"]) == 8

    # 3. old table preserved as chunks_old_<timestamp>.lance
    old_dirs = list(lancedb_path.glob(f"{me.OLD_TABLE_PREFIX}*.lance"))
    assert len(old_dirs) == 1
    # Verify old table is still openable and has the original dim
    table_names = me._list_table_names(db)
    old_tables = [t for t in table_names if t.startswith(me.OLD_TABLE_PREFIX)]
    assert len(old_tables) == 1
    old_table = db.open_table(old_tables[0])
    assert int(old_table.schema.field("vector").type.list_size) == 4
    assert old_table.count_rows() == 10

    # 4. staging table no longer present
    assert me.STAGING_TABLE not in table_names

    out = capsys.readouterr().out
    assert "commit summary" in out
    assert "migrated rows" in out and "10" in out
    assert "new vector dim" in out and "8" in out


def test_commit_refuses_when_staging_already_exists(tmp_path, stub_registry, capsys):
    """If chunks_v2 already exists, commit must refuse with exit code 3."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=5, dim=4,
                               profile="profile-from")
    # Pre-create chunks_v2 to simulate a previous interrupted run
    db = lancedb.connect(str(lancedb_path))
    db.create_table(
        me.STAGING_TABLE,
        data=[_make_row(99, 8, "new-model", "profile-to")],
        mode="create",
    )

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--commit",
        "--workspace", str(workspace),
    ])
    assert rc == 3
    # chunks_v2 must still be there — tool should NOT auto-delete it
    db2 = lancedb.connect(str(lancedb_path))
    assert me.STAGING_TABLE in me._list_table_names(db2)


def test_commit_idempotency_second_run_refuses_after_chunks_v2_leftover(
    tmp_path, stub_registry, capsys, monkeypatch,
):
    """If first commit fails and leaves chunks_v2 behind, second commit refuses."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=6, dim=4,
                               profile="profile-from")

    # Force the first commit to fail mid-rename by patching shutil.move
    import shutil
    real_move = shutil.move
    call_count = {"n": 0}

    def flaky_move(src, dst, *args, **kwargs):
        call_count["n"] += 1
        # Fail on the FIRST move call (which is chunks → chunks_old)
        if call_count["n"] == 1:
            raise OSError("simulated FS failure")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(me.shutil, "move", flaky_move)

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--commit",
        "--workspace", str(workspace),
    ])
    assert rc == 2  # runtime error
    # chunks_v2 should remain for forensics
    db = lancedb.connect(str(lancedb_path))
    table_names = me._list_table_names(db)
    assert me.STAGING_TABLE in table_names
    assert me.CHUNKS_TABLE in table_names  # original chunks still intact

    # Restore real shutil.move and try again — should refuse due to chunks_v2 existing
    monkeypatch.setattr(me.shutil, "move", real_move)
    rc2 = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--commit",
        "--workspace", str(workspace),
    ])
    assert rc2 == 3  # staging conflict


def test_commit_with_json_flag_emits_json(tmp_path, stub_registry, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _seed_table(workspace, "testbox", n_rows=4, dim=4, profile="profile-from")

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--commit",
        "--json",
        "--workspace", str(workspace),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # The last JSON object should be the summary
    # Find the first '{' after the markdown summary
    brace = out.rfind("{")
    assert brace != -1, "no JSON object found in stdout"
    summary = json.loads(out[brace:])
    assert summary["mode"] == "commit"
    assert summary["migrated_rows"] == 4
    assert summary["to_dim"] == 8


# --------------------------- error paths ---------------------------


def test_unknown_to_profile_exits_1(tmp_path, stub_registry, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _seed_table(workspace, "testbox", n_rows=3, dim=4, profile="profile-from")

    rc = me.main([
        "--container", "testbox",
        "--from", "profile-from",
        "--to", "ghost-profile",
        "--workspace", str(workspace),
    ])
    assert rc == 1


def test_unknown_from_profile_exits_1(tmp_path, stub_registry, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _seed_table(workspace, "testbox", n_rows=3, dim=4, profile="profile-from")

    rc = me.main([
        "--container", "testbox",
        "--from", "ghost-from",
        "--to", "profile-to",
        "--workspace", str(workspace),
    ])
    assert rc == 1


def test_container_directory_missing_exits_1(tmp_path, stub_registry, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # don't seed — container dir doesn't exist
    rc = me.main([
        "--container", "missingbox",
        "--from", "profile-from",
        "--to", "profile-to",
        "--workspace", str(workspace),
    ])
    assert rc == 1


def test_from_profile_dim_mismatch_exits_1(tmp_path, stub_registry, capsys):
    """If --from dim doesn't match the table schema dim, refuse."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Seed with dim=4 but pretend --from is profile-to (dim=8)
    _seed_table(workspace, "testbox", n_rows=3, dim=4, profile="profile-from")
    rc = me.main([
        "--container", "testbox",
        "--from", "profile-to",  # claims dim=8, but table is dim=4
        "--to", "profile-from",
        "--workspace", str(workspace),
    ])
    assert rc == 1


# --------------------------- helpers ---------------------------


def test_detect_table_dim(tmp_path, stub_registry):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=2, dim=4,
                               profile="profile-from")
    db = lancedb.connect(str(lancedb_path))
    t = db.open_table(me.CHUNKS_TABLE)
    assert me._detect_table_dim(t) == 4


def test_detect_row_profile(tmp_path, stub_registry):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lancedb_path = _seed_table(workspace, "testbox", n_rows=2, dim=4,
                               model="m-old", profile="profile-from")
    db = lancedb.connect(str(lancedb_path))
    t = db.open_table(me.CHUNKS_TABLE)
    model, profile, dim = me._detect_row_profile(t)
    assert model == "m-old"
    assert profile == "profile-from"
    assert dim == 4
