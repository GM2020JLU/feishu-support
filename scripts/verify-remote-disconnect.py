"""Real SSH disconnect canary; synthetic namespace processes, no remote files."""

import argparse
import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from importlib.resources import files
from uuid import uuid4


OBSERVER = r'''
import json,os,select,signal,sys,time
from pathlib import Path
root=int(sys.argv[1]); marker=sys.argv[2]
if marker.encode() not in Path('/proc/%s/cmdline'%root).read_bytes():
    raise RuntimeError('canary identity mismatch')
pids=[]; todo=[root]; fds=[]
try:
    while todo:
        pid=todo.pop()
        if pid in pids: continue
        if len(pids)>=16: raise RuntimeError('unexpected process tree size')
        fd=os.pidfd_open(pid); fds.append(fd); pids.append(pid)
        todo.extend(map(int,Path('/proc/%s/task/%s/children'%(pid,pid)).read_text().split()))
    sessions={os.getsid(pid) for pid in pids}
    if len(pids)<4 or len(sessions)<3:
        raise RuntimeError('namespace or setsid child missing')
    print(json.dumps({'armed':True,'process_count':len(pids),'sessions':len(sessions)}),flush=True)
    pending=list(fds); deadline=time.monotonic()+12
    while pending and time.monotonic()<deadline:
        ready,_,_=select.select(pending,[],[],min(.2,max(0,deadline-time.monotonic())))
        pending=[fd for fd in pending if fd not in ready]
    print(json.dumps({'all_exited':not pending,'process_count':len(pids),'still_running':len(pending)}),flush=True)
finally:
    # Failure cleanup targets only captured, non-reusable canary pidfds.
    for fd in reversed(fds):
        try: signal.pidfd_send_signal(fd,signal.SIGKILL)
        except ProcessLookupError: pass
        os.close(fd)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--mode", choices=("disconnect", "silence"), default="disconnect")
    args = parser.parse_args()
    if args.host.startswith("-"):
        parser.error("invalid host")
    marker = "k3-disconnect-"+uuid4().hex
    child = ("import os,time; pid=os.fork(); "
             "os.setsid() if pid==0 else None; "
             "print('READY',flush=True) if pid==0 else None; time.sleep(60)")
    sandbox = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
               "--unshare-ipc", "--unshare-net", "--clearenv"]
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        sandbox += ["--ro-bind-try", path, path]
    sandbox += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--setenv", "PATH", "/usr/bin:/bin",
                "--", "/usr/bin/python3", "-I", "-S", "-c", child, marker]
    payload = files("k3_support").joinpath("remote_guard_payload.py").read_text()
    payload = "import json,os; print(json.dumps({'guardian':os.getpid()}),flush=True)\n"+payload
    command = shlex.join(["/usr/bin/python3", "-I", "-S", "-c", payload, "exec "+shlex.join(sandbox)])
    children = []
    def start(command):
        process = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", args.host, command],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        children.append(process)
        messages = queue.Queue()
        def read():
            for line in iter(process.stdout.readline,b""):
                messages.put(line.decode().strip())
            messages.put(None)
        threading.Thread(target=read,daemon=True).start()
        return process,messages
    try:
        ssh, messages = start(command)
        ssh.stdin.write((str(int(time.time()*1000)+5000)+"\n").encode()); ssh.stdin.flush()
        first = messages.get(timeout=10)
        if first is None:
            raise RuntimeError(ssh.stderr.read().decode()[-1500:])
        guardian = json.loads(first)["guardian"]
        assert messages.get(timeout=5) == "READY"
        observer, observed = start(shlex.join(["/usr/bin/python3", "-I", "-S", "-c", OBSERVER, str(guardian), marker]))
        armed = observed.get(timeout=8)
        if armed is None:
            raise RuntimeError(observer.stderr.read().decode()[-1500:])
        assert json.loads(armed)["armed"]
        # Never send a remote stop command; either cut transport or stop pulsing.
        if args.mode == "disconnect":
            os.killpg(ssh.pid, signal.SIGKILL); ssh.wait(timeout=5)
        result = json.loads(observed.get(timeout=15))
        assert observer.wait(timeout=5) == 0
        if args.mode == "silence":
            assert ssh.wait(timeout=5) == 124
        print(json.dumps({**result,"host":args.host,"mode":args.mode,
                          "real_ssh_disconnected":args.mode == "disconnect","remote_files_created":False}))
        return 0 if result["all_exited"] else 1
    finally:
        for process in children:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL); process.wait(timeout=5)
            for stream in (process.stdin,process.stdout,process.stderr): stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
