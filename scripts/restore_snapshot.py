"""Restore a private migration snapshot into an existing EMPTY recovery database only."""

import argparse
import sys

from pathlib import Path

import pymysql
import sqlparse

from backend.core.conf import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--target-schema', required=True)
    args = parser.parse_args()
    target = args.target_schema
    if (target == settings.DATABASE_SCHEMA or not target.isidentifier()
            or not target.endswith(('_mcphub_qa', '_mcphub_recovery'))):
        parser.error('Target must differ from configured DB and end in _mcphub_qa or _mcphub_recovery')
    if settings.DATABASE_TYPE != 'mysql':
        parser.error('Only MySQL snapshots are supported')
    content = args.snapshot.read_text(encoding='utf-8')
    if not content.startswith('-- Private complete database snapshot.'):
        parser.error('Not a migration snapshot')
    conn = pymysql.connect(host=settings.DATABASE_HOST, port=settings.DATABASE_PORT,
                           user=settings.DATABASE_USER, password=settings.DATABASE_PASSWORD,
                           database=target, charset='utf8mb4', autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')
            if cur.fetchone()[0]:
                raise RuntimeError('Target is not empty; no restore attempted')
            for statement in sqlparse.split(content):
                # The backup writer emits only these forms. Never execute a USE,
                # DROP, DELETE or unrelated operator-supplied SQL from this command.
                cleaned = sqlparse.format(statement, strip_comments=True).strip().upper()
                if not cleaned.startswith((
                    'CREATE TABLE ', 'INSERT INTO ', 'SET NAMES ', 'SET FOREIGN_KEY_CHECKS=',
                    'SET SQL_MODE=', 'SET TIME_ZONE=',
                )):
                    raise RuntimeError('Unexpected snapshot statement; restore stopped')
                cur.execute(statement)
        print('Snapshot restored into separate database; production configuration unchanged')
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Restore failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
