#!/usr/bin/env python3
"""Check an explicitly installed CPU runtime inside the real networkless sandbox."""
import argparse
import json
from pathlib import Path

from k3_support.broker_process import run_process
from k3_support.replay_sandbox import sandbox_command


def verify(site_packages):
    program = (
        'import importlib.metadata as m\n'
        'import torch\n'
        'from FlagEmbedding import BGEM3FlagModel, FlagReranker\n'
        'import json\n'
        'assert m.version("FlagEmbedding") == "1.4.2"\n'
        'assert torch.__version__ == "2.8.0+cpu" and torch.version.cuda is None\n'
        'print(json.dumps({"flagembedding":m.version("FlagEmbedding"),'
        '"torch":torch.__version__,"cuda":torch.version.cuda,"imports_ok":True}))\n'
    )
    argv = sandbox_command(package=Path(__file__).resolve().parents[1] / 'src/k3_support',
                           site_packages=site_packages, program=program)
    pos = argv.index('--')
    argv[pos:pos] = ['--setenv', 'HF_HUB_OFFLINE', '1', '--setenv', 'TRANSFORMERS_OFFLINE', '1',
                     '--setenv', 'OMP_NUM_THREADS', '2']
    value = json.loads(run_process(argv=argv, cwd='/', env={}, stdin=b'', heartbeat=lambda: None,
                                  timeout=60, heartbeat_interval=1, output_limit=65536))
    return {**value, 'real_sandbox': True, 'weights_loaded': False, 'quality_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-packages', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.site_packages)))
