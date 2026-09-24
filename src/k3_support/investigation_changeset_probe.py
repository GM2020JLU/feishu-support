"""Appended to the fixed source sampler by the control plane, never by a model.

Captures immutable base-to-HEAD Git changes. This is a point-in-time source
observation, not a semantic repair verdict or a build/device proof.
"""

import base64

PATCH_LIMIT = 256 * 1024


def changeset(binding, source):
    root = Path(binding['path'])
    env = {'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8',
           'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':'/dev/null',
           'GIT_TERMINAL_PROMPT':'0', 'GIT_NO_REPLACE_OBJECTS':'1',
           'GIT_OPTIONAL_LOCKS':'0', 'GIT_ALLOW_PROTOCOL':'', 'GIT_NO_LAZY_FETCH':'1'}

    def git(*args):
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['/usr/bin/git','--no-pager','-c','core.fsmonitor=false',
                                     '-C',str(root),*args],env=env,stdout=output,
                                    stderr=subprocess.DEVNULL,timeout=30,check=False)
            if result.returncode or output.tell()>PATCH_LIMIT:
                raise ValueError('changeset_unavailable_or_too_large')
            output.seek(0)
            return output.read(PATCH_LIMIT+1)

    base, head = binding['base_commit'], source['head']
    if not source['base_is_ancestor'] or source['head']!=source['end_head']:
        raise ValueError('changeset_baseline_mismatch')
    commits = git('rev-list','--reverse','--topo-order',base+'..'+head).decode('ascii').splitlines()
    patch = git('diff','--no-ext-diff','--no-textconv','--binary','--full-index',
                '--no-renames',base,head,'--')
    paths = git('diff','--no-ext-diff','--no-textconv','--name-only','--no-renames','-z',base,head,'--')
    # No exclude-standard: ignored source/build files are also disclosed as
    # untracked. They are not represented by the committed patch.
    untracked = git('ls-files','--others','-z','--')
    second = sample(binding)
    if second != source or git('rev-parse','HEAD').decode().strip()!=head:
        raise ValueError('source_changed_during_changeset')
    return {'coverage':'complete_committed_changeset_v1','base_commit':base,
            'head_commit':head,'commits':commits,
            'paths_b64':base64.b64encode(paths).decode('ascii'),
            'patch_b64':base64.b64encode(patch).decode('ascii'),
            'patch_sha256':hashlib.sha256(patch).hexdigest(),
            'untracked_paths_b64':base64.b64encode(untracked).decode('ascii'),
            'tracked_content_matches':source['tracked_content_matches']}


def changeset_main():
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE,
                          (min(16*1024*1024,hard) if hard!=resource.RLIM_INFINITY else 16*1024*1024,hard))
        binding = json.loads(sys.argv[1])[0]
        source = sample(binding)
        value = {'sources':[source], 'changeset':{'state':'unavailable'}}
        try:
            value['changeset'] = {'state':'observed', **changeset(binding,source)}
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):
            pass
        print(json.dumps(value,ensure_ascii=True))
        return 0
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        print('{"error":"source_observation_unavailable"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(changeset_main())
