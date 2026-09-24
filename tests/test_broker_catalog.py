import json
import os
from uuid import uuid4

import pytest
from test_broker_claim import UID, queued
from test_broker_claim_receipts import KEY
from test_broker_dispatch import _rewrite_input
from test_review import active_config, result_text

from k3_support.broker_catalog import ActiveCatalog, Catalog
from k3_support.broker_claim_receipts import claim
from k3_support.broker_dispatch import dispatch_one
from k3_support.broker_input import read
from k3_support.broker_renew import renew
from k3_support.broker_results import submit
from k3_support.broker_start import authorize
from k3_support.broker_worker import run_one

AGENTS = ('codex', 'claude', 'dsh', 'opencode', 'hermes')


@pytest.fixture
def catalog(tmp_path):
    directory = tmp_path / 'profiles'
    directory.mkdir(mode=0o700)
    profiles = {}
    for agent in AGENTS:
        value = {'version': 2, 'agent': agent, 'provider': 'fixture', 'base_url': 'https://example.com',
                     'model': 'gpt-5.6-sol', 'reasoning': 'medium',
                     'wire_api': 'responses' if agent == 'codex' else 'messages' if agent == 'claude' else 'chat_completions'}
        (directory / f'{agent}.json').write_text(json.dumps(value))
        profiles[agent] = {'contract': f'{agent}.json', 'executable': f'/opt/{agent}/bin/{agent}',
                               'agent_home': f'/var/lib/worker/{agent}'}
    (directory / 'executors.json').write_text(json.dumps({'version': 1, 'profiles': profiles}))
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield Catalog(fd, control_uid=os.geteuid(), worker_uid=UID), directory
    finally:
        os.close(fd)


@pytest.mark.parametrize('agent', AGENTS)
def test_catalog_dispatch_claim_start_and_report_keep_selected_tool(conn, config, catalog, agent):
    source, _ = catalog
    policy = next(p.contract for p in source.profiles() if p.contract.agent == agent)
    queued(conn)
    _rewrite_input(conn, lambda payload: payload['context_extra'].update(execution=policy.selection()))
    cfg = active_config(config)
    reader = ActiveCatalog(conn, source)
    launch = dispatch_one(conn, cfg, contract_reader=reader, launch=lambda _: True)
    assert reader() == policy
    seen = []
    def transport(request):
        method = request['method']
        if method == 'claim':
            result = claim(conn, cfg, request, peer_uid=UID, control_key=KEY, contract_reader=reader)
        elif method == 'start':
            result = authorize(conn, cfg, request, peer_uid=UID, contract_reader=reader)
        elif method == 'renew':
            result = renew(conn, request, peer_uid=UID, config=cfg, contract_reader=reader)
        else:
            result = {'input': read, 'result': submit}[method](conn, request, peer_uid=UID)
        return {'version': 1, 'request_id': request['request_id'], 'ok': True, 'result': result}
    def execute(inputs, heartbeat):
        seen.append(inputs['execution']['agent'])
        heartbeat()
        return result_text('case-1')
    def select(inputs):
        return source.select(inputs['execution']['contract_fingerprint']).contract
    result = run_one(claim_request_id=launch['claim_request_id'], transport=transport,
                     executor=execute, select_contract=select)
    assert result['state'] == 'report_received' and seen == [agent]
    with pytest.raises(ValueError, match='already authorized'):
        run_one(claim_request_id=launch['claim_request_id'], transport=transport,
                executor=execute, select_contract=select)
    assert seen == [agent]
    assert conn.execute('SELECT count(*) FROM broker_results').fetchone()[0] == 1


def test_catalog_has_no_default_or_unlaunched_claim(conn, config, catalog):
    source, _ = catalog
    reader = ActiveCatalog(conn, source)
    with pytest.raises(ValueError):
        reader()
    queued(conn)
    cfg = active_config(config)
    # Old tasks have no immutable tool selection and require the old deployment.
    assert dispatch_one(conn, cfg, contract_reader=reader, launch=lambda _: pytest.fail('legacy launch')) == {'state': 'idle'}
    policy = source.profiles()[0].contract
    _rewrite_input(conn, lambda payload: payload['context_extra'].update(execution=policy.selection()))
    dispatch_one(conn, cfg, contract_reader=reader, launch=lambda _: True)
    with pytest.raises(ValueError, match='not dispatched'):
        claim(conn, cfg, {'version': 1, 'request_id': str(uuid4()), 'method': 'claim', 'params': {'pool': 'debug'}},
              peer_uid=UID, control_key=KEY, contract_reader=reader)
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == 'queued'


@pytest.mark.parametrize('problem', ['removed', 'changed', 'writable', 'duplicate', 'path', 'symlink', 'unknown_field'])
def test_catalog_changes_fail_closed_without_fallback(conn, config, catalog, problem):
    source, directory = catalog
    policy = source.profiles()[0].contract
    queued(conn)
    _rewrite_input(conn, lambda payload: payload['context_extra'].update(execution=policy.selection()))
    reader = ActiveCatalog(conn, source)
    dispatch_one(conn, active_config(config), contract_reader=reader, launch=lambda _: True)
    manifest = directory / 'executors.json'
    value = json.loads(manifest.read_text())
    if problem == 'removed':
        del value['profiles']['codex']
    elif problem == 'changed':
        path = directory / 'codex.json'
        contract = json.loads(path.read_text())
        contract['model'] = 'changed-model'
        path.write_text(json.dumps(contract))
    elif problem == 'writable':
        manifest.chmod(0o666)
    elif problem == 'duplicate':
        value['profiles']['copy'] = value['profiles']['codex']
    elif problem == 'path':
        value['profiles']['codex']['contract'] = '../codex.json'
    elif problem == 'symlink':
        path = directory / 'codex.json'
        path.rename(directory / 'hidden.json')
        path.symlink_to('hidden.json')
    else:
        value['profiles']['codex']['shell'] = 'arbitrary command'
    manifest.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        reader()
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == 'queued'


def test_global_priority_wins_over_catalog_order(conn, config, catalog):
    from test_broker_dispatch import _later_task

    from k3_support.ids import canonical_json, digest

    source, _ = catalog
    contracts = {p.contract.agent: p.contract for p in source.profiles()}
    queued(conn)
    _rewrite_input(conn, lambda payload: payload['context_extra'].update(execution=contracts['codex'].selection()))
    _later_task(conn)
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id='job-2'").fetchone()[0])
    payload['context_extra']['execution'] = contracts['hermes'].selection()
    conn.execute("UPDATE broker_inputs SET payload_json=? WHERE job_id='job-2'", (canonical_json(payload),))
    conn.execute("UPDATE jobs SET input_digest=? WHERE job_id='job-2'", (digest(payload),))
    reader = ActiveCatalog(conn, source)
    result = dispatch_one(conn, active_config(config), contract_reader=reader, launch=lambda _: False)
    assert result['state'] == 'unknown' and reader().agent == 'hermes'
    assert conn.execute('SELECT job_id FROM broker_launch_bindings').fetchone()[0] == 'job-2'
    assert dispatch_one(conn, active_config(config), contract_reader=reader,
                        launch=lambda _: pytest.fail('unknown launch must not retry')) == {'state': 'occupied'}


@pytest.mark.parametrize('value', ['http://user:secret@localhost:1234', 'http://localhost',
                                 'file:///tmp/socket', 'http://localhost:1234/path',
                                 'http://localhost:1234?key=x', 'http://localhost:1234#x',
                                 'http://localhost:0', 'http://localhost:65536', '\nhttp://localhost:1234'])
def test_proxy_profile_cannot_carry_credentials_or_arbitrary_settings(value):
    from k3_support.broker_catalog import proxy_environment

    with pytest.raises(ValueError):
        proxy_environment(value)


def test_catalog_proxy_is_explicit_and_optional(catalog):
    from k3_support.broker_catalog import proxy_environment

    source, directory = catalog
    assert all(p.proxy_url is None for p in source.profiles())
    path = directory / 'executors.json'
    value = json.loads(path.read_text())
    value['profiles']['codex']['proxy_url'] = 'http://127.0.0.1:12345'
    path.write_text(json.dumps(value))
    profile = source.profiles()[0]
    environment = proxy_environment(profile.proxy_url)
    assert environment['HTTPS_PROXY'] == profile.proxy_url
    assert environment['NO_PROXY'] == 'localhost,127.0.0.1,::1'
    assert proxy_environment(None) == {}
