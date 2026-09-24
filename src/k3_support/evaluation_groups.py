"""Candidate grouping only: no labels, promotion, model calls or private text export."""
from __future__ import annotations

import copy
import json

from .ids import digest


def parse_split_plan(raw: bytes) -> dict:
    if len(raw) > 1024 * 1024:
        raise ValueError('split plan exceeds 1 MiB')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate split plan key')
            value[key] = item
        return value

    return json.loads(raw, object_pairs_hook=unique)


def validate_split_plan(grouping: dict, plan: dict, *, candidate_digest: str) -> dict:
    """Validate an explicit plan, not approve its labels or semantic coverage."""
    if (not isinstance(plan, dict) or set(plan) != {'schema_version', 'candidate_digest', 'assignments'}
            or type(plan['schema_version']) is not int or plan['schema_version'] != 1
            or plan['candidate_digest'] != candidate_digest):
        raise ValueError('split plan does not match candidate batch')
    assignments = plan['assignments']
    expected = {identifier for group in grouping['groups'] for identifier in group['candidate_ids']}
    if (not isinstance(assignments, dict) or set(assignments) != expected
            or any(not isinstance(value, str) or value not in {'tuning', 'acceptance', 'excluded'}
                   for value in assignments.values())):
        raise ValueError('split plan must assign every candidate exactly once')
    for group in grouping['groups']:
        splits = {assignments[identifier] for identifier in group['candidate_ids']}
        if len(splits) != 1:
            raise ValueError('shared lineage cannot span different splits')
        if group['quarantined'] and splits != {'excluded'}:
            raise ValueError('unmapped candidates require topic review before scoring')
    return {'lineage_valid': True, 'candidate_digest': candidate_digest,
            'plan_digest': digest(plan),
            'counts': {name: sum(value == name for value in assignments.values())
                       for name in ('tuning', 'acceptance', 'excluded')},
            'semantic_leakage_verified': False, 'human_gold_approved': False,
            'release_eligible': False}


def group_candidates(candidates: list[dict]) -> dict:
    """Keep shared source/knowledge lineage together; quarantine unmapped rows.

    Connected components are necessary: a candidate may refer to several knowledge
    IDs and merge otherwise separate groups. This is not semantic topic discovery.
    Human reviewers must still check cross-topic paraphrases before assigning splits.
    """
    if not isinstance(candidates, list) or len(candidates) > 10000:
        raise ValueError('bounded candidate list required')
    rows = {}
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for row in candidates:
        identifier = row['id']
        if not isinstance(identifier, str) or not identifier or identifier in rows:
            raise ValueError('unique candidate IDs required')
        source = row['provenance']
        source_type, source_id = source['source_type'], source['source_id']
        if not all(isinstance(x, str) and x for x in (source_type, source_id)):
            raise ValueError('candidate source required')
        knowledge = row['suggestion']['allowed_knowledge_ids']
        if not isinstance(knowledge, list) or any(not isinstance(k, str) or not k for k in knowledge):
            raise ValueError('knowledge IDs must be strings')
        keys = [('source', source_type, source_id), *(('knowledge', k) for k in knowledge)]
        root = find(keys[0])
        for key in keys[1:]:
            parent[find(key)] = root
        rows[identifier] = (keys[0], bool(knowledge))
    groups = {}
    for identifier, (key, mapped) in rows.items():
        group = groups.setdefault(find(key), {'candidate_ids': [], 'all_mapped': True})
        group['candidate_ids'].append(identifier)
        group['all_mapped'] &= mapped
    result = []
    for group in groups.values():
        ids = sorted(group['candidate_ids'])
        result.append({'group_id': digest(ids), 'candidate_ids': ids,
                       'split': None, 'needs_topic_review': True,
                       'quarantined': not group['all_mapped']})
    return {'schema_version': 1, 'candidate_count': len(rows),
            'groups': sorted(result, key=lambda group: group['group_id']),
            'human_reviewed': False, 'split_assigned': False,
            'scope': 'source_and_knowledge_lineage_not_semantic_equivalence'}


def _reviewed_lineage(candidates, gold):
    reviewed = {item['id']: item for item in gold}
    combined = copy.deepcopy(candidates)
    for candidate in combined:
        label = reviewed.get(candidate['id'])
        if label is not None:
            ids = []
            for field in ('allowed_knowledge_ids', 'forbidden_knowledge_ids'):
                values = label.get(field, [])
                if not isinstance(values, list) or any(not isinstance(key, str) or not key for key in values):
                    raise ValueError('reviewed knowledge IDs must be strings')
                ids.extend(values)
            # A reviewed hard negative is linked to the knowledge it must not
            # answer with. This is lineage only, never reply permission.
            candidate['suggestion']['allowed_knowledge_ids'] = sorted(set(
                candidate['suggestion']['allowed_knowledge_ids'] + ids))
    return group_candidates(combined)


def require_acceptance_split(candidates, plan, *, candidate_digest, gold, lineage_gold=None):
    """Reject scoring a tuning/excluded row or an incomplete acceptance split."""
    grouping = _reviewed_lineage(candidates, gold if lineage_gold is None else lineage_gold)
    report = validate_split_plan(grouping, plan, candidate_digest=candidate_digest)
    expected = {key for key, value in plan['assignments'].items() if value == 'acceptance'}
    identifiers = [item['id'] for item in gold]
    if not expected or len(identifiers) != len(set(identifiers)) or set(identifiers) != expected:
        raise ValueError('gold must contain exactly the nonempty acceptance split')
    return report


def select_acceptance_gold(candidates, plan, *, candidate_digest, gold):
    """Select from an intact reviewed bundle; all its labels constrain lineage."""
    validate_split_plan(_reviewed_lineage(candidates, gold), plan, candidate_digest=candidate_digest)
    acceptance = [item for item in gold if plan['assignments'].get(item['id']) == 'acceptance']
    report = require_acceptance_split(candidates, plan, candidate_digest=candidate_digest,
                                     gold=acceptance, lineage_gold=gold)
    return acceptance, {**report, 'scored_ids_digest': digest(sorted(item['id'] for item in acceptance))}
