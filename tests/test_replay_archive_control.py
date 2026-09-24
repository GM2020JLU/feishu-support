import pytest

from k3_support import replay_archive_control as control


@pytest.mark.parametrize('name', ['../outside','/etc','a/b','a\\b','.','..','',None,'a\x00b'])
def test_control_rejects_paths_before_execution(tmp_path, monkeypatch, name):
    monkeypatch.setattr(control,'replay',lambda *a,**k: pytest.fail('must not execute'))
    with pytest.raises(ValueError):
        control.run_selected(tmp_path,name=name,manifest_digest='a'*64,confirmed=True)


def test_control_requires_confirmation_and_serializes_runs(tmp_path, monkeypatch):
    calls=[]
    def run(path,**kwargs):
        calls.append((path,kwargs))
        with pytest.raises(ValueError,match='already running'):
            control.run_selected(tmp_path,name='one',manifest_digest='a'*64,confirmed=True)
        return {'fixture':True}
    monkeypatch.setattr(control,'replay',run)
    for confirmed in (False,None,1,'true'):
        with pytest.raises(ValueError):
            control.run_selected(tmp_path,name='one',manifest_digest='a'*64,confirmed=confirmed)
    assert not calls
    assert control.run_selected(tmp_path,name='one',manifest_digest='a'*64,confirmed=True)=={'fixture':True}
    assert calls==[(tmp_path/'one',{'manifest_digest':'a'*64,'timeout':30})]
    # A completed run releases the single-process slot.
    control.run_selected(tmp_path,name='one',manifest_digest='a'*64,confirmed=True)


def test_failure_releases_slot_without_retry(tmp_path, monkeypatch):
    calls=[]
    def fail(*args,**kwargs):
        calls.append(1)
        raise TimeoutError('fixture timeout')
    monkeypatch.setattr(control,'replay',fail)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            control.run_selected(tmp_path,name='one',manifest_digest='a'*64,confirmed=True)
    assert calls==[1,1]
