"""Source drift must fail before spending or authorizing a coding launch."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config

from k3_support.broker_claim_receipts import claim
from k3_support.broker_start import authorize
from k3_support.config import Config
from k3_support.ids import canonical_json, digest
from k3_support.project_investigation_source import selection


def prepare(conn, config):
    cfg=active_config(config)
    queued(conn)
    payload=json.loads(conn.execute('SELECT payload_json FROM broker_inputs').fetchone()[0])
    payload['context_extra']['project_investigation']={'source':{
        **selection(cfg,'u-boot'),'branch':'main','base_commit':'a'*40,'version':''}}
    conn.execute('UPDATE broker_inputs SET payload_json=?',(canonical_json(payload),))
    conn.execute('UPDATE jobs SET input_digest=?',(digest(payload),))
    task=claim(conn,cfg,{'version':1,'request_id':str(uuid4()),'method':'claim','params':{'pool':'debug'}},
               peer_uid=UID,control_key=b't'*32,now=NOW)['task']
    return cfg,{'version':1,'request_id':str(uuid4()),'method':'start','params':task}


@pytest.mark.parametrize('change',['path','node','work_root'])
def test_drift_before_start_creates_no_permit_or_charge(conn,config,change):
    cfg,request=prepare(conn,config)
    raw=deepcopy(cfg.raw)
    if change=='path':raw['repositories']['u-boot']['path']+='-changed'
    elif change=='node':raw['runtime']['remote_host']='another-node'
    else:raw['runtime']['remote_worktree_root']+='-changed'
    before=list(conn.iterdump())
    with pytest.raises(ValueError,match='source deployment changed'):
        authorize(conn,Config(raw,cfg.path),request,peer_uid=UID,now=NOW)
    assert list(conn.iterdump())==before


def test_unchanged_source_gets_only_one_start(conn,config):
    cfg,request=prepare(conn,config)
    assert authorize(conn,cfg,request,peer_uid=UID,now=NOW)['accepted']
    with pytest.raises(ValueError,match='already authorized'):
        authorize(conn,cfg,request,peer_uid=UID,now=NOW)
    assert conn.execute('SELECT count(*) FROM broker_execution_starts').fetchone()[0]==1


def test_drift_during_instance_observation_keeps_committed_permit(conn,config):
    cfg,request=prepare(conn,config)
    def observe(*args,**kwargs):
        cfg.raw['repositories']['u-boot']['path']+='-changed'
    with pytest.raises(ValueError,match='source deployment changed'):
        authorize(conn,cfg,request,peer_uid=UID,now=NOW,observe_instance=observe)
    assert conn.execute('SELECT count(*) FROM broker_execution_starts').fetchone()[0]==1
    cfg.raw['repositories']['u-boot']['path']=cfg.raw['repositories']['u-boot']['path'].removesuffix('-changed')
    with pytest.raises(ValueError,match='already authorized'):
        authorize(conn,cfg,request,peer_uid=UID,now=NOW)


@pytest.mark.parametrize('change',['path','node','work_root','missing','database'])
def test_start_checks_actual_config_file(conn,config,change):
    import yaml

    from k3_support.project_investigation_source import monitor_source_config
    cfg,request=prepare(conn,config)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    monitored=monitor_source_config(cfg)
    raw=deepcopy(cfg.raw)
    if change=='path':raw['repositories']['u-boot']['path']+='-changed'
    elif change=='node':raw['runtime']['remote_host']='other-node'
    elif change=='work_root':raw['runtime']['remote_worktree_root']+='-changed'
    elif change=='database':raw['paths']['database']+='-different'
    if change=='missing':cfg.path.unlink()
    else:cfg.path.write_text(yaml.safe_dump(raw))
    before=list(conn.iterdump())
    with pytest.raises(ValueError):
        authorize(conn,monitored,request,peer_uid=UID,now=NOW)
    assert list(conn.iterdump())==before


def test_live_source_guard_accepts_unchanged_file(conn,config):
    import yaml

    from k3_support.project_investigation_source import monitor_source_config
    cfg,request=prepare(conn,config)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    assert authorize(conn,monitor_source_config(cfg),request,peer_uid=UID,now=NOW)['accepted']
