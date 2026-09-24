"""Read-only inventory for reference-index coverage, not a readiness decision."""

from dataclasses import asdict, dataclass
from collections import OrderedDict
from copy import deepcopy
import threading

from .ids import digest
from .retention_reference_extract import VERSION


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class Source:
    table: str
    column: str
    primary_key: tuple[str, ...]
    kinds: tuple[str, ...]
    schema_digest: str
    extractor_version: str = VERSION


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def inventory(conn):
    """Cache schema introspection only, keyed by exact current schema SQL.

    Never trust schema_version alone (rollback/recreate can reuse a version).
    Every call reads all sqlite_master definitions in its own consistent
    snapshot. Row contents, capture receipts and ready state are not cached.
    """
    conn.execute('SAVEPOINT reference_inventory')
    try:
        schema = [tuple(row) for row in conn.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        fingerprint = digest(schema)
        with _CACHE_LOCK:
            cached = _CACHE.get(conn)
            if cached and cached[0] == fingerprint:
                _CACHE.move_to_end(conn)
                return deepcopy(cached[1])
        report = _inventory_uncached(conn)
        with _CACHE_LOCK:
            _CACHE[conn] = (fingerprint, deepcopy(report))
            _CACHE.move_to_end(conn)
            while len(_CACHE) > 8:
                _CACHE.popitem(last=False)
        return report
    finally:
        conn.execute('RELEASE reference_inventory')


def _inventory_uncached(conn):
    """Discover sources before installing change capture.

    Unknown future schema changes change the manifest digest. No discovered
    source is automatically considered backfilled or authorized for deletion.
    Nullable key values still require rejection by row capture/backfill.
    """
    sources, issues, schemas = [], [], []
    for table, sql in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        columns = [tuple(row) for row in conn.execute(f'PRAGMA table_xinfo({identifier(table)})')]
        foreign_keys = [tuple(row) for row in conn.execute(f'PRAGMA foreign_key_list({identifier(table)})')]
        schema = {'table': table, 'sql': sql, 'columns': columns, 'foreign_keys': foreign_keys}
        schemas.append(schema)
        keys = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
        kinds = {}
        for row in columns:
            if row[1].endswith('_json'):
                kinds.setdefault(row[1], set()).add('json')
        for fk in foreign_keys:
            if fk[2] == 'inbound_events' and fk[4] in ('event_pk', None):
                kinds.setdefault(fk[3], set()).add('inbound_fk')
        if table in ('case_sources', 'knowledge_sources'):
            if 'stable_external_id' not in {row[1] for row in columns}:
                issues.append({'table': table, 'reason': 'missing_stable_external_id'})
            else:
                kinds.setdefault('stable_external_id', set()).add('external_id')
        for column, semantics in sorted(kinds.items()):
            source = Source(table, column, keys, tuple(sorted(semantics)), digest(schema))
            sources.append(source)
            if not keys:
                issues.append({'table': table, 'column': column, 'reason': 'missing_stable_primary_key'})
            if sql and sql.lstrip().upper().startswith('CREATE VIRTUAL TABLE'):
                issues.append({'table': table, 'column': column, 'reason': 'unsupported_virtual_source'})
    return {
        'sources': sources,
        'issues': issues,
        'schema_digest': digest(schemas),
        'coverage_digest': digest([asdict(source) for source in sources]),
        'extractor_version': VERSION,
        'ready': False,
    }
