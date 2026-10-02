"""Real-MySQL regression: run ONLY against an isolated restored *_mcphub_qa database."""

import argparse
import hashlib
import subprocess
import sys

from pathlib import Path

from backend.core.conf import settings
from backend.plugin.mcphub.scripts.migrate import connection, quote


def fingerprints(conn):
    with conn.cursor() as cur:
        cur.execute('SHOW TABLES')
        tables = [row[0] for row in cur.fetchall()]
        result = {}
        for table in tables:
            cur.execute('SELECT * FROM ' + quote(table))
            # Hashes are compared in memory only. No secrets or row fingerprints are logged.
            rows = sorted(hashlib.sha256(repr(row).encode()).digest() for row in cur.fetchall())
            result[table] = hashlib.sha256(b''.join(rows)).digest()
    conn.rollback()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    if not settings.DATABASE_SCHEMA.endswith('_mcphub_qa'):
        parser.error('Refusing non-QA database')
    base = [sys.executable, '-m', 'backend.plugin.mcphub.scripts.migrate']
    conn = connection()
    try:
        before = fingerprints(conn)
        subprocess.run([*base, 'upgrade', '--backup-dir', str(args.backup_dir)], check=True)
        first = fingerprints(conn)
        for table, digest in before.items():
            if table not in ('sys_menu', 'sys_role_menu') and not table.startswith('mcphub_'):
                assert first[table] == digest, 'Existing data changed: ' + table
        with conn.cursor() as cur:
            for name in ('PluginMcpHub', 'ManageMcpHub'):
                cur.execute('SELECT COUNT(*) FROM sys_menu WHERE name=%s AND deleted=0', (name,))
                assert cur.fetchone()[0] == 1, 'Menu seed duplication'
            cur.execute("SELECT COUNT(*) FROM sys_role_menu rm JOIN sys_menu m ON m.id=rm.menu_id "
                        "WHERE m.name='ManageMcpHub'")
            assert cur.fetchone()[0] == 0, 'Migration granted manager permissions'
        conn.rollback()
        subprocess.run([*base, 'upgrade', '--backup-dir', str(args.backup_dir)], check=True)
        assert fingerprints(conn) == first, 'Repeated upgrade changed database contents'
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE mcphub_key ADD COLUMN qa_unexpected INT NULL')
        try:
            drifted = fingerprints(conn)
            failure = subprocess.run([*base, 'upgrade', '--backup-dir', str(args.backup_dir)], capture_output=True)
            assert failure.returncode != 0, 'Unknown schema drift was silently accepted'
            assert fingerprints(conn) == drifted, 'Failed drift preflight changed stored data'
        finally:
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE mcphub_key DROP COLUMN qa_unexpected')
        subprocess.run([*base, 'verify'], check=True)
        print('PASS: legacy rows preserved; least-privilege menus; idempotent upgrade; drift refused')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
