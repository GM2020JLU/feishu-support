import pytest

from k3_support.evaluation_groups import (
    group_candidates,
    parse_split_plan,
    validate_split_plan,
)


def row(identifier, source, knowledge):
    return {'id': identifier, 'query': 'PRIVATE QUESTION',
            'provenance': {'source_type': 'approved_knowledge', 'source_id': source},
            'suggestion': {'allowed_knowledge_ids': knowledge}}


@pytest.mark.parametrize('ids,valid', [(['a'], True), (['b'], False), (['a', 'b'], False),
                                      ([], False), (['a', 'a'], False)])
def test_acceptance_scoring_rejects_tuning_and_partial_sets(ids, valid):
    from k3_support.evaluation_groups import require_acceptance_split

    candidates = [row('a', 's1', ['k1']), row('b', 's2', ['k2'])]
    plan = {'schema_version': 1, 'candidate_digest': 'fixture',
            'assignments': {'a': 'acceptance', 'b': 'tuning'}}
    kwargs = dict(candidate_digest='fixture', gold=[{'id': key} for key in ids])
    if valid:
        assert require_acceptance_split(candidates, plan, **kwargs)['lineage_valid']
    else:
        with pytest.raises(ValueError, match='acceptance split'):
            require_acceptance_split(candidates, plan, **kwargs)


def test_transitive_lineage_stays_together_and_order_is_stable():
    rows = [row('a', 's1', ['k1']), row('b', 's2', ['k1', 'k2']), row('c', 's3', ['k2'])]
    result = group_candidates(rows)
    assert result == group_candidates(rows[::-1])
    assert result['groups'][0]['candidate_ids'] == ['a', 'b', 'c']
    assert 'PRIVATE' not in str(result)
    assert not result['human_reviewed'] and not result['split_assigned']


def test_shared_source_and_unmapped_candidate_quarantine_whole_group():
    result = group_candidates([row('a', 's1', ['k1']), row('b', 's1', [])])
    assert len(result['groups']) == 1 and result['groups'][0]['quarantined']
    assert result['groups'][0]['split'] is None


def test_duplicate_candidate_rejected():
    with pytest.raises(ValueError):
        group_candidates([row('a', 's1', []), row('a', 's2', [])])


def test_reviewed_label_cannot_bridge_acceptance_into_tuning():
    import copy

    from k3_support.evaluation_groups import require_acceptance_split

    candidates = [row('a', 's1', ['k1']), row('b', 's2', ['k2'])]
    original = copy.deepcopy(candidates)
    plan = {'schema_version': 1, 'candidate_digest': 'fixture',
            'assignments': {'a': 'acceptance', 'b': 'tuning'}}
    with pytest.raises(ValueError, match='shared lineage'):
        require_acceptance_split(candidates, plan, candidate_digest='fixture',
            gold=[{'id': 'a', 'allowed_knowledge_ids': ['k2']}])
    assert candidates == original


@pytest.mark.parametrize('positive_split', ['acceptance', 'tuning'])
def test_reviewed_hard_negative_uses_forbidden_knowledge_as_lineage(positive_split):
    from k3_support.evaluation_groups import require_acceptance_split

    candidates = [row('negative', 's1', []), row('positive', 's2', ['k1'])]
    plan = {'schema_version': 1, 'candidate_digest': 'fixture',
            'assignments': {'negative': 'acceptance', 'positive': positive_split}}
    gold = [{'id': 'negative', 'allowed_knowledge_ids': [], 'forbidden_knowledge_ids': ['k1']}]
    if positive_split == 'acceptance':
        gold.append({'id': 'positive', 'allowed_knowledge_ids': ['k1']})
        result = require_acceptance_split(candidates, plan, candidate_digest='fixture', gold=gold)
        assert result['counts']['acceptance'] == 2
        assert not result['release_eligible']
        assert candidates[0]['suggestion']['allowed_knowledge_ids'] == []
        assert gold[0]['allowed_knowledge_ids'] == []
    else:
        with pytest.raises(ValueError, match='shared lineage'):
            require_acceptance_split(candidates, plan, candidate_digest='fixture', gold=gold)


def test_select_acceptance_preserves_full_review_and_checks_tuning_labels():
    import copy

    from k3_support.evaluation_groups import select_acceptance_gold

    candidates = [row('a', 's1', ['k1']), row('b', 's2', ['k2'])]
    plan = {'schema_version': 1, 'candidate_digest': 'fixture',
            'assignments': {'a': 'acceptance', 'b': 'tuning'}}
    gold = [{'id': 'a', 'allowed_knowledge_ids': ['k1']},
            {'id': 'b', 'allowed_knowledge_ids': ['k2']}]
    original = copy.deepcopy(gold)
    selected, report = select_acceptance_gold(candidates, plan, candidate_digest='fixture', gold=gold)
    assert selected == [gold[0]] and report['counts']['tuning'] == 1
    assert gold == original
    gold[1]['allowed_knowledge_ids'] = ['k1']
    with pytest.raises(ValueError, match='shared lineage'):
        select_acceptance_gold(candidates, plan, candidate_digest='fixture', gold=gold)


@pytest.mark.parametrize('assignments', [
    {'a': 'tuning', 'b': 'acceptance', 'c': 'excluded'},
    {'a': 'tuning', 'b': 'tuning', 'c': 'acceptance'},
    {'a': 'tuning', 'b': 'tuning'},
    {'a': 'tuning', 'b': 'tuning', 'c': []},
])
def test_split_plan_rejects_leakage_unmapped_and_incomplete(assignments):
    grouping = group_candidates([row('a', 's1', ['k']), row('b', 's2', ['k']), row('c', 's3', [])])
    with pytest.raises(ValueError):
        validate_split_plan(grouping, {'schema_version': 1, 'candidate_digest': 'fixture',
                                      'assignments': assignments}, candidate_digest='fixture')


def test_cli_split_validation_keeps_labels_and_release_unapproved(monkeypatch):
    from argparse import Namespace

    from k3_support import cli, knowledge_gold_review

    monkeypatch.setattr(knowledge_gold_review, '_load_jsonl', lambda *a, **k: ([row('a', 's', ['k'])], 'fixture'))
    monkeypatch.setattr(knowledge_gold_review, '_private_regular_file', lambda *a, **k:
                        b'{"schema_version":1,"candidate_digest":"fixture","assignments":{"a":"tuning"}}')
    output = []
    monkeypatch.setattr(cli, '_emit', output.append)
    cli.cmd_knowledge_gold_groups(Namespace(candidates='fixture', split_plan='fixture-plan'))
    assert output[0]['lineage_valid'] and not output[0]['release_eligible']
    assert not output[0]['human_gold_approved']


def test_duplicate_json_assignments_rejected():
    with pytest.raises(ValueError, match='duplicate'):
        parse_split_plan(b'{"assignments":{"a":"tuning","a":"acceptance"}}')
