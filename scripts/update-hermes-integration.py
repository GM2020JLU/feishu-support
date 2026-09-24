#!/usr/bin/env python3
"""Update registered Hermes links through the standard package installer.

Run with Hermes gateway stopped. Never follow or replace an unrelated link.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


def update(root: Path, hermes: Path) -> None:
    root = root.resolve(strict=True)
    hermes = hermes.resolve(strict=True)
    binary = (root / 'releases/current/venv/bin/k3-supportctl').resolve(strict=True)
    config = root / 'instance/config.yaml'
    pairs = [
        (hermes / 'plugins/k3-support-control', root / 'instance/integrations/hermes-plugin'),
        (hermes / 'skills/software-development/k3-support-orchestrator', root / 'instance/integrations/hermes-skill'),
    ]
    state = subprocess.run(['systemctl', '--user', 'show', 'hermes-gateway.service',
                            '--property=ActiveState', '--value'], capture_output=True, text=True, check=True)
    if state.stdout.strip() not in {'inactive', 'failed'}:
        raise RuntimeError('Stop hermes-gateway.service before updating its integration')
    for registered, canonical in pairs:
        if registered.is_symlink():
            if registered.resolve(strict=True) != canonical or canonical.is_symlink() or not canonical.is_dir():
                raise RuntimeError('Unexpected integration link; no files changed')
        elif registered.exists() or canonical.exists():
            raise RuntimeError('Unmanaged integration directory; no files changed')
    moved = []
    try:
        for registered, canonical in pairs:
            registered.parent.mkdir(parents=True, exist_ok=True)
            canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if registered.is_symlink():
                registered.unlink()
                try:
                    canonical.rename(registered)
                except BaseException:
                    registered.symlink_to(canonical, target_is_directory=True)
                    raise
            moved.append((registered, canonical))
        subprocess.run([str(binary), '--config', str(config), 'hermes-plugin-install',
                        '--hermes-home', str(hermes), '--apply'], check=True)
    finally:
        for registered, canonical in reversed(moved):
            if registered.is_dir() and not registered.is_symlink() and not canonical.exists():
                registered.rename(canonical)
                registered.symlink_to(canonical, target_is_directory=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--hermes-home', type=Path, default=Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))))
    args = parser.parse_args()
    update(args.root, args.hermes_home)
