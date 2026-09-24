"""Fixed read-only Git baseline probe, run by control with python -I -S."""

import json
import os
import subprocess
import sys
import tempfile


def sample(binding):
    path = binding['path']
    if not path.startswith('/') or os.path.realpath(path) != path:
        raise ValueError('noncanonical_source')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.split('/')[1:]:
            if not part or part in ('.', '..'):
                raise ValueError('invalid_source')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1',
               'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
               'GIT_NO_REPLACE_OBJECTS':'1','GIT_OPTIONAL_LOCKS':'0',
               'GIT_ALLOW_PROTOCOL':'','GIT_NO_LAZY_FETCH':'1'}

        def git(*args, allow_false=False):
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    ['/usr/bin/git','--no-pager','-c','core.fsmonitor=false',
                     '-C',f'/proc/self/fd/{fd}',*args], env=env, stdout=output,
                    stderr=subprocess.DEVNULL, timeout=15, pass_fds=(fd,), check=False)
                if allow_false and result.returncode == 1:
                    return False
                if result.returncode or output.tell() > 4096:
                    raise ValueError('git_probe_failed')
                output.seek(0)
                value = output.read(4097).decode().strip()
                return True if allow_false else value

        if git('rev-parse','--show-toplevel') != path:
            raise ValueError('not_repository_root')
        branch = binding['branch']
        ref = branch if branch.startswith('refs/heads/') else 'refs/heads/' + branch
        first = git('rev-parse','--verify',ref+'^{commit}')
        base = git('rev-parse','--verify',binding['base_commit']+'^{commit}')
        ancestor = git('merge-base','--is-ancestor',base,first,allow_false=True)
        last = git('rev-parse','--verify',ref+'^{commit}')
        return {'repository':binding['repository'],'path':path,'branch':branch,
                'base_commit':base,'branch_commit':first,'end_branch_commit':last,
                'base_is_ancestor':ancestor,'coverage':'source_repository_baseline'}
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        print(json.dumps(sample(json.loads(sys.argv[1])),sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        print('{"error":"baseline_probe_unavailable"}')
        sys.exit(1)
