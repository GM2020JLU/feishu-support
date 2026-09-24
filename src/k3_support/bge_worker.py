"""Finite BGE inference protocol for a supervised networkless process."""
import contextlib
import json
import os
import re
import sys

MAX_INPUT = 262144
MAX_OUTPUT = 2 * 1024 * 1024


def validate(request):
    identities = {'embedding_digest', 'embedding_revision', 'reranker_digest', 'reranker_revision'}
    if not isinstance(request, dict) or set(request) - {'query'} != identities | {'operation', 'texts'}:
        raise ValueError('invalid BGE request fields')
    for name in identities:
        length = 64 if name.endswith('digest') else 40
        if not isinstance(request[name], str) or not re.fullmatch(f'[a-f0-9]{{{length}}}', request[name]):
            raise ValueError('invalid pinned model identity')
    if request['operation'] not in {'encode', 'rerank'}:
        raise ValueError('unsupported BGE operation')
    if (not isinstance(request['texts'], list) or not 1 <= len(request['texts']) <= 16
            or any(not isinstance(text, str) or not 1 <= len(text) <= 4096 for text in request['texts'])):
        raise ValueError('BGE requires 1..16 bounded texts')
    if request['operation'] == 'rerank':
        if not isinstance(request.get('query'), str) or not 1 <= len(request['query']) <= 4096:
            raise ValueError('bounded rerank query required')
    elif 'query' in request:
        raise ValueError('encode does not accept a query')


def execute(request):
    validate(request)
    from .bge_local import load

    provider = load(embedding_root='/models/embedding', reranker_root='/models/reranker',
                    operation=request['operation'],
                    **{key: value for key, value in request.items() if key.endswith(('_digest', '_revision'))})
    result = (provider.encode(request['texts']) if request['operation'] == 'encode'
              else provider.rerank(request['query'], request['texts']))
    return {'operation': request['operation'], 'result': result, 'binding': provider.binding,
            'quality_verified': False}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError('input limit exceeded')
        request = json.loads(raw)
        # Third-party diagnostics may contain input text. Never return them to
        # the parent as a protocol error or ordinary process output.
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            value = execute(request)
        output = json.dumps(value, allow_nan=False).encode()
        if len(output) > MAX_OUTPUT:
            raise ValueError('output limit exceeded')
        sys.stdout.buffer.write(output)
        return 0
    except Exception:
        print('BGE worker failed; no result accepted.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
