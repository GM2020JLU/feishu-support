"""One-shot local BGE sandbox; caller supplies trusted dependency/model mounts."""
import json
import math
from pathlib import Path

from .bge_worker import MAX_INPUT, MAX_OUTPUT, validate
from .broker_process import run_process
from .replay_sandbox import sandbox_command


def validate_response(value, request):
    def number(item):
        return type(item) in (int, float) and math.isfinite(item)

    if (not isinstance(value, dict) or set(value) != {'operation', 'result', 'binding', 'quality_verified'}
            or value['operation'] != request['operation'] or value['quality_verified'] is not False
            or not isinstance(value['result'], list) or len(value['result']) != len(request['texts'])):
        raise ValueError('invalid BGE worker response')
    binding = value['binding']
    if not isinstance(binding, dict):
        raise ValueError('invalid BGE worker binding')
    for key in ('embedding', 'reranker'):
        identity = binding.get(key)
        if (not isinstance(identity, dict)
                or identity.get('artifact_digest') != request[key + '_digest']
                or identity.get('revision') != request[key + '_revision']
                or identity.get('verification') != 'declared_only'):
            raise ValueError('BGE worker identity mismatch')
    for item in value['result']:
        if request['operation'] == 'rerank':
            if not number(item) or not 0 <= item <= 1:
                raise ValueError('invalid BGE rerank score')
            continue
        if not isinstance(item, dict) or set(item) != {'dense', 'sparse'}:
            raise ValueError('invalid BGE vector')
        dense, sparse = item['dense'], item['sparse']
        if (not isinstance(dense, list) or len(dense) != 1024
                or not all(number(v) for v in dense) or not any(dense)
                or not isinstance(sparse, dict) or set(sparse) != {'indices', 'values'}):
            raise ValueError('invalid BGE vector')
        indices, weights = sparse['indices'], sparse['values']
        if (not isinstance(indices, list) or not isinstance(weights, list)
                or not indices or len(indices) != len(weights)
                or any(type(i) is not int or not 0 <= i < 2**32 for i in indices)
                or indices != sorted(set(indices))
                or any(not number(w) or w <= 0 for w in weights)):
            raise ValueError('invalid BGE sparse vector')
    return value


def run(request, *, site_packages, embedding_root, reranker_root, timeout=120):
    validate(request)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError('BGE timeout must be finite and at most 300 seconds')
    payload = json.dumps(request, allow_nan=False).encode()
    if len(payload) > MAX_INPUT:
        raise ValueError('BGE input exceeds limit')
    mounts = []
    for value, destination in [(embedding_root, '/models/embedding'), (reranker_root, '/models/reranker')]:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir() or '..' in path.parts:
            raise ValueError('real absolute model directory required')
        path = path.resolve(strict=True)
        if path in (Path('/'), Path.home()) or Path.home().is_relative_to(path):
            raise ValueError('refusing broad model mount')
        mounts += ['--ro-bind', str(path), destination]
    program = ('import resource\n'
               'resource.setrlimit(resource.RLIMIT_AS, (12884901888, 12884901888))\n'
               'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n'
               'from k3_support.bge_worker import main\nraise SystemExit(main())\n')
    argv = sandbox_command(package=Path(__file__).parent, site_packages=Path(site_packages), program=program)
    additions = ['--dir', '/models', *mounts]
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE'):
        additions += ['--setenv', key, '1']
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        additions += ['--setenv', key, '2']
    separator = argv.index('--')
    argv[separator:separator] = additions
    output = run_process(argv=argv, cwd='/', env={}, stdin=payload, heartbeat=lambda: None,
                         timeout=timeout, heartbeat_interval=min(1, timeout), output_limit=MAX_OUTPUT)
    value = json.loads(output)
    return validate_response(value, request)
