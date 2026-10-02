"""Launch the existing FBA CLI, with locked dependencies and migrated MCP schema."""

import argparse
import os
import subprocess
import sys

from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    os.chdir(root)
    os.environ['FBA_PLUGIN_DEPENDENCIES_LOCKED'] = '1'
    subprocess.run(
        [sys.executable, '-m', 'backend.plugin.mcphub.scripts.migrate', 'verify'], check=True,
    )
    # Keep the existing Granian/FBA run implementation and no-reload process shape.
    # No uv sync, pip install, automatic schema migration or background shell here.
    cli = Path(sys.executable).parent / 'fba'
    os.execv(sys.executable, [
        sys.executable, str(cli), 'run', '--no-reload', '--workers', '1',
        '--host', args.host, '--port', str(args.port),
    ])


if __name__ == '__main__':
    main()
