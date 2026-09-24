import copy

import pytest

from k3_support.replay_transcript import VerificationTranscript


def row():
    return {'argv': ['ssh', 'fixture', 'git status'], 'cwd': None, 'timeout': 30,
            'returncode': 0, 'stdout': 'fixture', 'stderr': ''}


def test_exact_transcript_consumes_once_and_copies_input():
    value = row()
    transcript = VerificationTranscript([value])
    value['stdout'] = 'changed'
    result = transcript(['ssh', 'fixture', 'git status'], None, 30)
    assert result.stdout == 'fixture'
    assert transcript.status()['remaining'] == 0
    with pytest.raises(ValueError, match='exhausted'):
        transcript(['ssh', 'fixture', 'git status'], None, 30)


@pytest.mark.parametrize('field,value', [('argv', ['other']), ('cwd', '/different'), ('timeout', 31)])
def test_mismatch_permanently_invalidates_transcript(field, value):
    original = row()
    transcript = VerificationTranscript([original])
    call = copy.deepcopy(original)
    call[field] = value
    with pytest.raises(ValueError, match='match'):
        transcript(call['argv'], call['cwd'], call['timeout'])
    with pytest.raises(ValueError, match='invalidated'):
        transcript(original['argv'], original['cwd'], original['timeout'])
    assert transcript.status()['consumed'] == 0


@pytest.mark.parametrize('field,value', [('argv', 'ssh host'), ('argv', ['a\x00b']),
    ('timeout', True), ('returncode', False), ('stdout', {}), ('stderr', 'x' * 200001)])
def test_invalid_transcript_rejected(field, value):
    original = row()
    original[field] = value
    with pytest.raises(ValueError):
        VerificationTranscript([original])
