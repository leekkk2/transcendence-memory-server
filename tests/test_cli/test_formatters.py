"""Lightweight checks on the formatter helpers (no network)."""

from __future__ import annotations

import io

from tm_cli.formatters import (
    OutputMode,
    build_console,
    emit_json,
    render_containers_table,
    render_search_hits,
)


def test_emit_json_writes_compact_unicode(capsys):
    emit_json({"hello": "你好"})
    captured = capsys.readouterr()
    assert "你好" in captured.out


def test_render_search_hits_smoke(capsys):
    mode = OutputMode(no_color=True)
    console = build_console(mode, file=io.StringIO())
    render_search_hits(
        [{"score": 0.5, "text": "alpha"}],
        console=console,
        query="q",
        container="c",
        topk=1,
    )
    out = console.file.getvalue()
    assert "alpha" in out


def test_render_containers_table_smoke():
    mode = OutputMode(no_color=True)
    console = build_console(mode, file=io.StringIO())
    render_containers_table(
        [{"name": "foo", "objects": 3, "last_modified": "2026-01-01", "index_state": "fresh"}],
        console=console,
    )
    out = console.file.getvalue()
    assert "foo" in out
    assert "fresh" in out
