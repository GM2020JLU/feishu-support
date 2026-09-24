import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def layout(tmp_path):
    spec = importlib.util.spec_from_file_location('unified_update', Path(__file__).resolve().parents[1]/'scripts/update-hermes-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, hermes = tmp_path/'project', tmp_path/'hermes'
    binary=root/'releases/current/venv/bin/k3-supportctl'
    binary.parent.mkdir(parents=True)
    binary.touch()
    pairs = [(hermes/'plugins/k3-support-control',root/'instance/integrations/hermes-plugin'),
             (hermes/'skills/software-development/k3-support-orchestrator',root/'instance/integrations/hermes-skill')]
    for registered, canonical in pairs:
        canonical.mkdir(parents=True)
        (canonical/'original').write_text('preserved')
        registered.parent.mkdir(parents=True)
        registered.symlink_to(canonical, target_is_directory=True)
    return module,root,hermes,pairs


@pytest.mark.parametrize('fail', [False,True])
def test_install_restores_single_canonical_copy(layout,monkeypatch,fail):
    module,root,hermes,pairs=layout
    def run(argv,**kwargs):
        if argv[0]=='systemctl': return SimpleNamespace(stdout='inactive\n')
        for registered,canonical in pairs:
            assert registered.is_dir() and not registered.is_symlink()
            assert not canonical.exists()
        if fail: raise RuntimeError('synthetic install failure')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess,'run',run)
    if fail:
        with pytest.raises(RuntimeError,match='synthetic'): module.update(root,hermes)
    else: module.update(root,hermes)
    for registered,canonical in pairs:
        assert registered.is_symlink() and registered.resolve()==canonical
        assert (canonical/'original').read_text()=='preserved'


def test_running_gateway_does_not_mutate_links(layout,monkeypatch):
    module,root,hermes,pairs=layout
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='active\n'))
    with pytest.raises(RuntimeError,match='Stop'): module.update(root,hermes)
    assert all(p.is_symlink() for p,_ in pairs)


def test_unrelated_symlink_is_not_followed(layout,monkeypatch,tmp_path):
    module,root,hermes,pairs=layout
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='inactive\n'))
    registered,_=pairs[1]
    other=tmp_path/'unrelated'
    other.mkdir()
    registered.unlink()
    registered.symlink_to(other)
    with pytest.raises(RuntimeError,match='Unexpected'): module.update(root,hermes)
    assert pairs[0][0].is_symlink() and registered.resolve()==other
