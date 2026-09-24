"""Appended to the fixed source sampler; prepare then exec a job command."""

# This module is only executed concatenated after verification_source_probe.py.

import json
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def checkout_main(binding, command, source_sampler):
    root = Path(binding['root'])
    repo = root / 'repository'
    if Path.cwd() != root or root.resolve(strict=True) != root:
        raise ValueError('workspace_changed')
    env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1',
           'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
           'GIT_NO_REPLACE_OBJECTS':'1','GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1'}

    def git(*args, clone=False):
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
                 *args],
                cwd=root,env={**env,'GIT_ALLOW_PROTOCOL':'file' if clone else ''},
                stdout=output,stderr=subprocess.DEVNULL,timeout=600,check=False)
            if result.returncode or output.tell()>4096:
                raise ValueError('checkout_command_failed')
            output.seek(0)
            return output.read(4097).decode().strip()

    def validate_binding(item, *, primary=False):
        if not isinstance(item, dict):
            raise ValueError('checkout_binding_invalid')
        name = item.get('repository')
        source_path = item.get('source_path')
        base = item.get('base_commit')
        if not isinstance(name, str) or not name or not isinstance(source_path, str) or not source_path:
            raise ValueError('checkout_binding_invalid')
        if not isinstance(base, str) or len(base) not in (40, 64) or any(c not in '0123456789abcdef' for c in base):
            raise ValueError('checkout_binding_invalid')
        rel = 'repository' if primary else item.get('relative_path')
        if not primary and rel != 'repositories/' + hashlib.sha256(name.encode()).hexdigest()[:16]:
            raise ValueError('checkout_path_invalid')
        target = root / rel
        # Refuse a symlink at any existing component. The relative paths are fixed
        # above, but an attacker could have replaced the parent directory.
        if not target.is_relative_to(root):
            raise ValueError('checkout_path_invalid')
        cursor = root
        for component in Path(rel).parts:
            cursor = cursor / component
            if os.path.lexists(cursor) and cursor.is_symlink():
                raise ValueError('independent_checkout_required')
        seed_job_id = item.get('seed_job_id')
        if seed_job_id is not None:
            seed = source_sampler({'repository': name, 'path': source_path,
                                   'base_commit': base, 'candidate_commit': base})
            if not seed['matched']:
                raise ValueError('candidate_source_changed')
        return target, rel

    primary_target, _ = validate_binding(binding, primary=True)
    companions = binding.get('companions', [])
    if not isinstance(companions, list):
        raise ValueError('checkout_binding_invalid')
    names = {binding['repository']}
    companion_targets = []
    for item in companions:
        target, rel = validate_binding(item)
        if item['repository'] in names:
            raise ValueError('checkout_binding_invalid')
        names.add(item['repository'])
        companion_targets.append((item, target, rel))

    def prepare_source(item, target, *, companion=False):
        created = not os.path.lexists(target)
        if created and binding['continuation']:
            raise ValueError('existing_workspace_missing')
        if created:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.parent.is_symlink() or target.parent.resolve(strict=True) != target.parent:
                raise ValueError('independent_checkout_required')
            git('clone','--template=','--no-hardlinks','--dissociate','--no-checkout','--no-recurse-submodules',
                '--',item['source_path'],str(target),clone=True)
            git('-C',str(target),'checkout','--detach',item['base_commit'])
        if (target.is_symlink() or target.resolve(strict=True) != target
                or not (target/'.git').is_dir() or (target/'.git').is_symlink()):
            raise ValueError('independent_checkout_required')
        alternates = target/'.git/objects/info/alternates'
        if os.path.lexists(alternates):
            raise ValueError('shared_object_database_rejected')
        head = git('-C',str(target),'rev-parse','HEAD')
        observed = source_sampler({'repository':item['repository'],'path':str(target),
                                   'base_commit':item['base_commit'],'candidate_commit':head})
        if not observed['base_is_ancestor'] or observed['head'] != observed['end_head']:
            raise ValueError('checkout_lineage_changed')
        if companion:
            if (head != item['base_commit'] or not observed['tracked_content_matches']
                    or not observed['matched']):
                raise ValueError('joint_checkout_not_clean_baseline')
        elif not binding['continuation'] and (head != item['base_commit'] or not observed['tracked_content_matches']):
            raise ValueError('initial_checkout_not_clean_baseline')
        return created, observed

    created, observed = prepare_source(binding, primary_target)
    companion_observations = []
    for item, target, _ in companion_targets:
        _, companion_observed = prepare_source(item, target, companion=True)
        companion_observations.append(companion_observed)
    receipt = {'request_id':binding['request_id'],'job_id':binding['job_id'],
               'base_commit':binding['base_commit'],'created':created,'source':observed}
    if 'companions' in binding:
        receipt['companions'] = companion_observations
    print('K3_CHECKOUT_V1:'+json.dumps(receipt,separators=(',',':')),flush=True)
    os.environ['K3_VERIFICATION_SOURCES'] = json.dumps(
        {binding['repository']:str(primary_target), **{
            item['repository']:str(target) for item, target, _ in companion_targets}},
        separators=(',',':'))
    os.chdir(primary_target)
    os.execv('/bin/bash',['/bin/bash','--noprofile','--norc','-c',command])


if __name__ == '__main__':
    try:
        args = json.loads(sys.argv[1])
        checkout_main(args['binding'],args['command'],globals()['sample'])
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        print('investigation checkout unavailable; existing files preserved',file=sys.stderr)
        sys.exit(126)
