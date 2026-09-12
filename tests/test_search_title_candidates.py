"""Title recall regressions against real, isolated LanceDB tables."""
from __future__ import annotations

import lancedb
import pyarrow as pa
import pytest

from scripts.search_candidates import search_candidates


def _table(tmp_path, rows, *, with_title=True):
    fields = [
        pa.field('taskId', pa.string()),
        pa.field('chunkId', pa.string()),
        pa.field('text', pa.string()),
        pa.field('vector', pa.list_(pa.float32(), 2)),
    ]
    if with_title:
        fields.append(pa.field('title', pa.string()))
    data = pa.Table.from_pylist(rows, schema=pa.schema(fields))
    return lancedb.connect(str(tmp_path / 'db')).create_table('chunks', data)


def _row(name, title, vector):
    return {'taskId': name, 'chunkId': name + ':0', 'title': title, 'text': 'body', 'vector': vector}


def test_title_outside_dense_topk_enters_candidates_with_real_distance(tmp_path):
    query = 'aws-eva 磁盘清理转存网盘与文件索引'
    table = _table(tmp_path, [
        *[_row(f'noise-{i}', '历史运维记录', [i / 100, 0.0]) for i in range(30)],
        _row('target', query + ' @ 2026-09-11 实战', [3.0, 4.0]),
    ])
    assert 'target' not in {r['taskId'] for r in table.search([0., 0.]).limit(20).to_list()}

    rows = search_candidates(table, [0., 0.], query, 20)

    assert len(rows) == 21
    target = next(r for r in rows if r['taskId'] == 'target')
    assert target['_distance'] == pytest.approx(25.0)


def test_exact_title_is_reserved_before_many_substring_matches(tmp_path):
    table = _table(tmp_path, [
        *[_row(f'noise-{i}', 'other', [i / 100, 0.0]) for i in range(30)],
        *[_row(f'phrase-{i}', f'release notes {i}', [2., 0.]) for i in range(40)],
        _row('exact', 'Release Notes', [3., 4.]),
    ])

    rows = search_candidates(table, [0., 0.], 'release notes', 30)

    assert len(rows) == 50  # 30 dense + at most 20 title candidates
    assert rows[30]['taskId'] == 'exact'
    assert len({r['chunkId'] for r in rows}) == len(rows)


@pytest.mark.parametrize('query,title', [
    ('  AWS-EVA 磁盘清理  ', 'aws-eva 磁盘清理 实战'),
    ("O'Brien 100%_backup\\node", "O'Brien 100%_backup\\node 实战"),
    ("' OR true --", "literal ' OR true -- title"),
])
def test_title_filter_is_case_insensitive_and_treats_query_literally(tmp_path, query, title):
    table = _table(tmp_path, [
        _row('dense', None, [0., 0.]),
        _row('decoy', "O'Brien 100XXbackup\\node", [1., 0.]),
        _row('target', title, [3., 4.]),
    ])

    rows = search_candidates(table, [0., 0.], query, 1)

    assert [r['taskId'] for r in rows] == ['dense', 'target']


def test_dense_title_overlap_is_not_duplicated(tmp_path):
    table = _table(tmp_path, [_row('target', 'a title', [0., 0.])])
    assert len(search_candidates(table, [0., 0.], 'a title', 5)) == 1


@pytest.mark.parametrize('query,with_title', [('missing', True), ('  ', True), ('title', False)])
def test_no_title_matches_or_legacy_schema_preserves_dense_results(tmp_path, query, with_title):
    table = _table(tmp_path, [
        _row('first', 'title', [0., 0.]),
        _row('second', None, [1., 0.]),
    ], with_title=with_title)
    expected = table.search([0., 0.]).metric('l2').limit(1).to_list()
    assert search_candidates(table, [0., 0.], query, 1) == expected


def test_subprocess_search_entrypoint_uses_title_candidates(tmp_path, monkeypatch):
    from scripts import task_rag_search

    query = '磁盘清理'
    _table(tmp_path, [
        _row('dense', 'other', [0., 0.]),
        _row('target', query, [3., 4.]),
    ])
    monkeypatch.setattr(task_rag_search, 'embed_text', lambda _: [0., 0.])
    monkeypatch.setattr(task_rag_search, 'lancedb_dir', lambda _: tmp_path / 'db')

    payload = task_rag_search.search_lancedb(query, 1, 'main')

    assert payload['code'] == 'ok'
    assert [r['taskId'] for r in payload['results']] == ['dense', 'target']
    assert payload['results'][1]['score'] == pytest.approx(25.0)
    assert all('vector' not in r for r in payload['results'])
