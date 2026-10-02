"""Non-destructive, versioned MySQL migration. Never imports backend.main."""

import argparse
import hashlib
import json
import os
import sys

from datetime import datetime, timezone
from pathlib import Path

import pymysql
import sqlparse

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy.dialects import mysql

from backend.core.conf import settings
from backend.plugin.mcphub.model.entities import (
    McpGrant, McpKey, McpKeyBinding, McpRequest, McpServer, McpTool,
)

TABLES = (McpServer.__table__, McpTool.__table__, McpRequest.__table__,
          McpGrant.__table__, McpKey.__table__, McpKeyBinding.__table__)
REVISION = '20261002_01'
SQL_DIR = Path(__file__).resolve().parents[1] / 'sql' / 'mysql'
MENU_NAMES = ('PluginMcpHub', 'ManageMcpHub')


def connection():
    if settings.DATABASE_TYPE != 'mysql':
        raise RuntimeError('This migration supports MySQL only')
    return pymysql.connect(
        host=settings.DATABASE_HOST, port=settings.DATABASE_PORT,
        user=settings.DATABASE_USER, password=settings.DATABASE_PASSWORD,
        database=settings.DATABASE_SCHEMA, charset='utf8mb4', autocommit=False,
    )


def quote(name):
    return '`' + name.replace('`', '``') + '`'


def backup(conn, directory):
    """Write a complete consistent InnoDB snapshot, without logging row contents."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ('mcphub-before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sql')
    with conn.cursor() as cur:
        cur.execute('SELECT TABLE_NAME, ENGINE, TABLE_TYPE FROM information_schema.tables WHERE TABLE_SCHEMA=DATABASE()')
        tables = cur.fetchall()
        if any(kind != 'BASE TABLE' or engine != 'InnoDB' for _, engine, kind in tables):
            raise RuntimeError('Snapshot requires InnoDB base tables only; use an operator-reviewed native backup')
        cur.execute("SET time_zone='+00:00'")
        cur.execute('SELECT @@SESSION.sql_mode')
        sql_mode = cur.fetchone()[0]
        # Metadata reads may start a transaction with autocommit disabled.
        # End that read-only transaction before selecting snapshot isolation.
        conn.rollback()
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write('-- Private complete database snapshot. Restore ONLY into an empty separate database.\n')
            out.write('SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n')
            out.write('SET SQL_MODE=' + conn.escape(sql_mode) + ";\nSET TIME_ZONE='+00:00';\n")
            for name, _, _ in tables:
                with conn.cursor() as cur:
                    cur.execute('SHOW CREATE TABLE ' + quote(name))
                    out.write(cur.fetchone()[1] + ';\n')
                with conn.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute('SELECT * FROM ' + quote(name))
                    columns = ','.join(quote(c[0]) for c in cur.description)
                    for row in cur:
                        values = ','.join(
                            "X'" + value.hex() + "'" if isinstance(value, bytes) else conn.escape(value)
                            for value in row
                        )
                        out.write(f'INSERT INTO {quote(name)} ({columns}) VALUES ({values});\n')
            out.write('SET FOREIGN_KEY_CHECKS=1;\n')
            out.flush()
            os.fsync(out.fileno())
        conn.rollback()
    except BaseException:
        conn.rollback()
        path.unlink(missing_ok=True)
        raise
    print(json.dumps({'backup': str(path), 'tables': len(tables), 'bytes': path.stat().st_size}))
    return path


def shape_check(engine, *, allow_missing):
    """Fail on drift; CREATE IF NOT EXISTS is never treated as an ALTER migration."""
    schema = inspect(engine)
    present = set(schema.get_table_names())
    for table in TABLES:
        if table.name not in present:
            if allow_missing:
                continue
            raise RuntimeError('Missing table: ' + table.name)
        actual = {c['name']: c for c in schema.get_columns(table.name)}
        if set(actual) != set(table.columns.keys()):
            raise RuntimeError('Column drift: ' + table.name + '; explicit ALTER revision required')
        for col in table.columns:
            observed = actual[col.name]
            expected_type = str(col.type.compile(dialect=mysql.dialect())).upper()
            actual_type = str(observed['type']).upper()
            # MySQL reflects BOOLEAN as TINYINT; display widths carry no schema semantics.
            if expected_type == 'BOOL' or expected_type == 'BOOLEAN':
                expected_type = 'TINYINT'
            if actual_type == 'TINYINT(1)':
                actual_type = 'TINYINT'
            if actual_type != expected_type or observed['nullable'] != col.nullable:
                raise RuntimeError('Type/nullability drift: ' + table.name + '.' + col.name)
        if schema.get_pk_constraint(table.name)['constrained_columns'] != ['id']:
            raise RuntimeError('Primary key drift: ' + table.name)
        unique = {tuple(u['column_names']) for u in schema.get_unique_constraints(table.name)}
        expected_unique = {tuple(c.name for c in u.columns) for u in table.constraints
                           if u.__class__.__name__ == 'UniqueConstraint'}
        if not expected_unique.issubset(unique):
            raise RuntimeError('Unique constraint drift: ' + table.name)
        actual_fks = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']))
                      for f in schema.get_foreign_keys(table.name)}
        expected_fks = {(tuple(e.parent.name for e in f.elements), f.elements[0].column.table.name,
                         tuple(e.column.name for e in f.elements)) for f in table.foreign_key_constraints}
        if actual_fks != expected_fks:
            raise RuntimeError('Foreign key drift: ' + table.name)
        indexed = {tuple(i['column_names']) for i in schema.get_indexes(table.name)}
        if not {tuple(c.name for c in i.columns) for i in table.indexes}.issubset(indexed):
            raise RuntimeError('Index drift: ' + table.name)


def menu_preflight(conn):
    with conn.cursor() as cur:
        for identifier, name in zip((86202610020001, 86202610020002), MENU_NAMES):
            cur.execute('SELECT name,deleted FROM sys_menu WHERE id=%s', (identifier,))
            row = cur.fetchone()
            if row and row != (name, 0):
                raise RuntimeError('Reserved MCP menu ID collision; no changes applied')
            cur.execute('SELECT COUNT(*) FROM sys_menu WHERE name=%s AND deleted=0', (name,))
            if cur.fetchone()[0] > 1:
                raise RuntimeError('Duplicate MCP menu names; manual reconciliation required')
        cur.execute("SELECT component,path,parent_id FROM sys_menu WHERE name='PluginMcpHub' AND deleted=0")
        row = cur.fetchone()
        if row and row != ('/plugins/mcphub/views/index', '/plugins/mcphub', None):
            raise RuntimeError('Existing MCP menu differs from route contract')
    conn.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'verify', 'backup', 'upgrade'))
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    if args.action in ('backup', 'upgrade') and args.backup_dir is None:
        parser.error('--backup-dir is required for backup/upgrade')
    if settings.DATABASE_TYPE != 'mysql':
        parser.error('Only MySQL is supported; no database has been modified')
    sql_path = SQL_DIR / ('init_snowflake.sql' if settings.DATABASE_PK_MODE == 'snowflake' else 'init.sql')
    sql = sql_path.read_text(encoding='utf-8')
    digest = hashlib.sha256(sql.encode()).hexdigest()
    engine = create_engine(URL.create('mysql+pymysql', username=settings.DATABASE_USER,
                           password=settings.DATABASE_PASSWORD, host=settings.DATABASE_HOST,
                           port=settings.DATABASE_PORT, database=settings.DATABASE_SCHEMA), echo=False)
    conn = connection()
    lock = False
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s, 0)', ('mcphub:' + hashlib.sha256(settings.DATABASE_SCHEMA.encode()).hexdigest()[:40],))
            lock = cur.fetchone()[0] == 1
            if not lock:
                raise RuntimeError('Another MCP migration owns the database lock')
        menu_preflight(conn)
        shape_check(engine, allow_missing=True)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='mcphub_schema_revision'")
            ledger_exists = bool(cur.fetchone()[0])
            revision = None
            if ledger_exists:
                cur.execute('SELECT checksum FROM mcphub_schema_revision WHERE revision=%s', (REVISION,))
                revision = cur.fetchone()
                if revision and revision[0] != digest:
                    raise RuntimeError('Applied revision checksum mismatch; add a new explicit migration')
        conn.rollback()
        print(json.dumps({'revision': REVISION, 'applied': bool(revision), 'action': args.action,
                          'schema_tables': [t.name for t in TABLES], 'legacy_key_changes': False}))
        if args.action in ('plan', 'verify'):
            if args.action == 'verify' and not revision:
                raise RuntimeError('MCP schema revision is not applied; run backed-up upgrade before startup')
            if revision:
                shape_check(engine, allow_missing=False)
            return
        backup(conn, args.backup_dir)
        if args.action == 'backup':
            return
        if revision:
            shape_check(engine, allow_missing=False)
            print('Revision already applied; no writes performed')
            return
        # MySQL DDL commits implicitly. Each statement is observable and rerunnable;
        # no claim of whole-migration transactional rollback is made.
        for number, statement in enumerate(sqlparse.split(sql), 1):
            with conn.cursor() as cur:
                cur.execute(statement)
            conn.commit()
            print(json.dumps({'statement_committed': number}))
        shape_check(engine, allow_missing=False)
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS mcphub_schema_revision ('
                        'revision VARCHAR(32) PRIMARY KEY, checksum CHAR(64) NOT NULL, '
                        'applied_at DATETIME NOT NULL) ENGINE=InnoDB')
            cur.execute('INSERT INTO mcphub_schema_revision VALUES (%s,%s,UTC_TIMESTAMP())', (REVISION, digest))
        conn.commit()
        print('Upgrade committed; legacy users, passwords and API keys unchanged')
    finally:
        if lock:
            with conn.cursor() as cur:
                cur.execute('SELECT RELEASE_LOCK(%s)', ('mcphub:' + hashlib.sha256(settings.DATABASE_SCHEMA.encode()).hexdigest()[:40],))
        conn.close()
        engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Driver errors can include server/account details; never dump connection exceptions.
        print('Migration failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
