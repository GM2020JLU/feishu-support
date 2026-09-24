import pytest

from k3_support.case_reference_graph import descendants
from k3_support.store import create_case


def make_case(conn):
    return create_case(conn, title='private', case_type='faq', severity='P3', confidence=.9)[0]


def test_transitive_composite_references_and_cycles_are_deduplicated(conn):
    case = make_case(conn)
    other = make_case(conn)
    conn.executescript('''
        CREATE TABLE graph_parent(a TEXT,b TEXT,case_id TEXT REFERENCES cases(case_id),
                                  private_body TEXT,PRIMARY KEY(a,b));
        CREATE TABLE graph_child(id TEXT PRIMARY KEY,a TEXT,b TEXT,
                                  FOREIGN KEY(a,b) REFERENCES graph_parent(a,b));
        CREATE TABLE graph_cycle(id TEXT PRIMARY KEY,child TEXT REFERENCES graph_child(id),
                                  parent TEXT REFERENCES graph_cycle(id));
    ''')
    conn.execute("INSERT INTO graph_parent VALUES('same','yes',?,'SECRET')", (case,))
    conn.execute("INSERT INTO graph_parent VALUES('same','no',?,'OTHER')", (other,))
    conn.execute("INSERT INTO graph_child VALUES('included','same','yes')")
    conn.execute("INSERT INTO graph_child VALUES('excluded','same','no')")
    conn.execute("INSERT INTO graph_cycle VALUES('cycle','included','cycle')")
    before = conn.serialize()
    result = descendants(conn, case)
    rows = {r['table']: r['rows'] for r in result['tables']}
    assert rows['graph_parent'] == rows['graph_child'] == rows['graph_cycle'] == 1
    assert not result['truncated'] and not result['unresolved_foreign_keys']
    assert result['unsupported_tables'] == []
    assert result['declared_fk_scan_complete']
    assert not result['coverage_complete'] and not result['deletion_allowed']
    assert 'SECRET' not in str(result) and 'included' not in str(result)
    assert conn.serialize() == before


def test_row_budget_and_unsupported_identity_do_not_claim_complete(conn):
    case = make_case(conn)
    result = descendants(conn, case, max_rows=1)
    assert result['rows'] == 1 and result['truncated']
    assert not result['declared_fk_scan_complete']
    conn.execute('CREATE TABLE no_row(rowid TEXT, _rowid_ TEXT, oid TEXT)')
    result = descendants(conn, case)
    assert 'no_row' in result['unsupported_tables']
    assert not result['declared_fk_scan_complete']


def test_without_rowid_composite_parent_and_shadowed_alias(conn):
    case = make_case(conn)
    conn.execute('''CREATE TABLE compact(a TEXT,b INTEGER,case_id TEXT REFERENCES cases,
                   PRIMARY KEY(a,b)) WITHOUT ROWID''')
    conn.execute('''CREATE TABLE leaf(rowid TEXT,a TEXT,b INTEGER,
                   FOREIGN KEY(a,b) REFERENCES compact)''')
    conn.execute("INSERT INTO compact VALUES('key',1,?)", (case,))
    conn.execute("INSERT INTO leaf VALUES('not-a-row-identity','key',1)")
    result = descendants(conn, case)
    counts = {r['table']: r['rows'] for r in result['tables']}
    assert counts['compact'] == counts['leaf'] == 1
    assert result['declared_fk_scan_complete']


def test_parent_collation_and_table_case_match_sqlite_foreign_keys(conn):
    case = make_case(conn)
    conn.execute('''CREATE TABLE MixedParent(id TEXT COLLATE NOCASE PRIMARY KEY,
                   case_id TEXT REFERENCES CASES(case_id))''')
    conn.execute('''CREATE TABLE mixed_child(id TEXT PRIMARY KEY,
                   parent TEXT COLLATE BINARY REFERENCES mixedparent(id))''')
    conn.execute("INSERT INTO MixedParent VALUES('MiXeD',?)", (case,))
    conn.execute("INSERT INTO mixed_child VALUES('child','mixed')")
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    result = descendants(conn, case)
    counts = {r['table']: r['rows'] for r in result['tables']}
    assert counts['MixedParent'] == counts['mixed_child'] == 1
    assert result['declared_fk_scan_complete']


@pytest.mark.parametrize('limit', [True, 0, 10001, '2'])
def test_invalid_budgets(conn, limit):
    with pytest.raises(ValueError):
        descendants(conn, 'case', max_rows=limit)
