"""Disposable subordinate-UID control server for the no-model MCP canary."""

import json
import os
import pwd
import selectors
import subprocess
import sys
import sysconfig
from contextlib import contextmanager
from pathlib import Path

import yaml


CHILD = r'''
import json, os, socket, sys, tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from k3_support.broker_connection import serve_connection
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_grants import issue, revoke
from k3_support.approvals import request_approval, decide_approval, normalized_board_action
from k3_support.config import Config
from k3_support.db import connect, migrate, transaction
from k3_support.ids import canonical_json,digest
from k3_support.store import create_case

args=json.loads(sys.stdin.readline())
root=Path(args['root'])
with tempfile.TemporaryDirectory(prefix='private-control-',dir=root) as private:
    conn=connect(Path(private)/'fixture.db')
    try:
        migrate(conn)
        case,_=create_case(conn,title='synthetic MCP canary',case_type='bug',severity='P3',confidence=.8)
        conn.execute("UPDATE cases SET state='board_testing'")
        now=datetime.now(UTC); stamp=now.isoformat()
        payload={'case_id':case,'lifecycle_round':1,'brief':'synthetic','repos':['u-boot'],'model':'gpt-5.6-sol','reasoning':'medium','context_extra':{'board_session_id':'synthetic-session'}}
        fingerprint=digest(payload)
        conn.execute("INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,input_digest,attempt_no,available_at,created_at,updated_at) VALUES('fixture',?,'codex','running','fixture',?,?,1,?,?,?)",
                     (case,(now+timedelta(minutes=5)).isoformat(),fingerprint,stamp,stamp,stamp))
        conn.execute('INSERT INTO broker_inputs VALUES(?,?,?)',('fixture',canonical_json(payload),stamp))
        uid=args['peer_uid']
        with transaction(conn):
            grant=issue(conn,job_id='fixture',attempt_no=1,lease_owner='fixture',worker_uid=uid)
        task={'job_id':'fixture','case_id':case,'execution_round':1,'lifecycle_round':1,'input_digest':fingerprint,'lease_token':grant['token']}
        descriptor=ExecutionContract('fixture','https://example.com','gpt-5.6-sol','medium','responses','a'*64)
        conn.execute('INSERT INTO broker_execution_starts VALUES(?,?,?,?,?,?)',(grant['grant_id'],'fixture',1,str(uuid4()),uid,stamp))
        conn.execute('INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)',(grant['grant_id'],descriptor.fingerprint,descriptor.provider,descriptor.model,stamp))
        target=str(uuid4())
        conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,'succeeded',?,?)",(target,uid,grant['grant_id'],'fixture','{}',stamp,stamp))
        conn.execute('INSERT INTO broker_remote_results VALUES(?,?,?,?,?)',(target,0,'synthetic-authorized-output','',stamp))
        conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,'succeeded',?,?)",
                     (target,uid,grant['grant_id'],'synthetic-session','fixture','{}',descriptor.fingerprint,'fixture',stamp,stamp))
        conn.execute('INSERT INTO broker_board_results VALUES(?,?,?,?,?)',(target,0,'synthetic-board-output','',stamp))
        raw=args['config']; raw['mode']='active'; raw['features']['codex']=True
        raw['features']['board']=True
        raw['identity']['telegram_control_user_id']='fixture-owner'
        raw['identity']['telegram_control_chat_id']='fixture-chat'
        cfg=Config(raw,Path(private)/'unused.yaml')
        approval,fingerprint,_=request_approval(conn,approval_type='board1_lease',case_id=case,session_id='synthetic-session',
            action=normalized_board_action(case,'synthetic-session',5),expires_at=(now+timedelta(minutes=5)).isoformat())
        decide_approval(conn,cfg,approval_id=approval,approve=True,approver_user_id='fixture-owner',approver_chat_id='fixture-chat',
            message_id='synthetic-approval',decision_text='approve board',expected_digest=fingerprint)
        path=str(root/'broker.sock')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
            listener.bind(path); os.chmod(path,0o666); listener.listen(2); listener.settimeout(30)
            print(json.dumps({'task':task,'target':target,'socket':path}),flush=True)
            for index in range(7):
                if index == 5: revoke(conn,grant_id=grant['grant_id'])
                peer,_=listener.accept()
                serve_connection(conn,peer,worker_uid=uid,config=cfg,contract_reader=lambda:descriptor)
            assert conn.execute("SELECT count(*) FROM broker_board_actions WHERE state='queued'").fetchone()[0] == 1
            assert conn.execute('SELECT count(*) FROM action_ledger').fetchone()[0] == 0
        os.unlink(path)
    finally:
        conn.close()
'''


@contextmanager
def control_fixture(root):
    user = pwd.getpwuid(os.geteuid()).pw_name
    ranges = []
    for path in ("/etc/subuid", "/etc/subgid"):
        fields = next(line.split(":") for line in Path(path).read_text().splitlines()
                      if line.split(":")[0] in (user, str(os.geteuid())))
        ranges.append((int(fields[1]), int(fields[2])))
    root.chmod(0o733)
    source = Path(__file__).resolve().parents[1]
    fds = [os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in
           (sys.base_prefix, sysconfig.get_path("purelib"), source / "src")]
    process = None
    try:
        runtime, packages, package_source = [f"/proc/self/fd/{fd}" for fd in fds]
        command = ["unshare", "--user", "--map-users", f"0:{ranges[0][0]}:{ranges[0][1]}",
                   "--map-groups", f"0:{ranges[1][0]}:{ranges[1][1]}", "--setuid", "1", "--setgid", "1",
                   runtime + "/bin/python3", "-c", CHILD]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=fds, cwd="/tmp", env={"PYTHONHOME": runtime, "PYTHONPATH": packages+":"+package_source,
                                   "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        process.stdin.write((json.dumps({"root": str(root), "peer_uid": int(Path('/proc/sys/kernel/overflowuid').read_text()),
                                        "config": yaml.safe_load((source / 'config/config.example.yaml').read_text())})+"\n").encode())
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(15):
                raise TimeoutError("fixture startup")
            line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read().decode()[-2000:])
        binding = json.loads(line)
        private = next(root.glob("private-control-*"))
        try:
            (private / "fixture.db").read_bytes()
        except PermissionError:
            pass
        else:
            raise RuntimeError("worker identity can read the synthetic control database")
        yield {"K3_SUPPORT_BROKER_TASK": json.dumps(binding['task']), "K3_SUPPORT_BROKER_SOCKET": binding['socket'],
               "K3_SUPPORT_BROKER_CONTROL_UID": str(ranges[0][0]+1)}, binding['target']
        if process.wait(timeout=10) != 0:
            raise RuntimeError(process.stderr.read().decode()[-2000:])
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for fd in fds:
            os.close(fd)
