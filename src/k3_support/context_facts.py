"""Deterministic, source-bound conversation facts, not verified measurements."""

from __future__ import annotations

import re

from .ids import digest
from .scope_facts import _QUOTED, SCOPE_KEYS, analyze_scope_facts

CONTEXT_FACT_POLICY = "conversation-facts-v3"
_CORRECTION = re.compile(
    r"不是|并非|不再|而是|现在|目前|当前|实际|纠正|说错|换成|改为|\b(?:now|actually|instead|correction|not|currently)\b",
    re.IGNORECASE,
)
_PERSONAL = re.compile(r"我这里|我的|我这块|\bmy\b", re.IGNORECASE)
_TEST_BOARD = re.compile(r"(?<![a-z0-9_])board1(?![a-z0-9_])|测试板", re.IGNORECASE)
_SITE = re.compile(r"现场|客户那边|同事那边|对方那边", re.IGNORECASE)


def _subjects(content, event, requester_id):
    # Preserve source offsets while excluding quoted examples as subject cues.
    masked = _QUOTED.sub(lambda match: " " * len(match.group()), content)
    subject = "case_site"
    clauses = []
    for match in re.finditer(r"[^，,;；。!！?？\n]+", masked):
        text = match.group()
        board = bool(_TEST_BOARD.search(text))
        personal = bool(_PERSONAL.search(text))
        site = bool(_SITE.search(text))
        if board and (personal or site):
            subject = "ambiguous"
        elif board:
            subject = "test_board:board1"
        elif site:
            subject = "case_site"
        elif personal:
            subject = (
                "case_site"
                if event["sender_id"] == requester_id
                else "caller:" + str(event["sender_id"])
            )
        clauses.append(
            (match.start(), match.end(), subject, bool(_CORRECTION.search(text)))
        )
    return clauses


def project_facts(events: list[dict], *, query: str, requester_id: str | None) -> dict:
    mentions = []
    for event in events:
        content = str(event["content"])
        parsed = analyze_scope_facts(content)
        clauses = _subjects(content, event, requester_id)
        for mention in parsed["mentions"]:
            start = mention["source"]["start"]
            component = None
            if mention['field'] == 'software_version':
                clause = next((content[left:right] for left, right, _, _ in clauses
                               if left <= start < right), '')
                clause = _QUOTED.sub(lambda match: ' '*len(match.group()), clause)
                matches = re.findall(r'(?<![a-z0-9_])(?:u-?boot\s+spl|u-?boot|spl|linux|opensbi|edk2|esos)(?![a-z0-9_])', clause, re.I)
                components = {'spl' if re.fullmatch(r'u-?boot\s+spl', m, re.I)
                              else 'u-boot' if re.fullmatch(r'u-?boot', m, re.I)
                              else m.lower() for m in matches}
                if len(components) == 1:
                    component = components.pop()
            subject, correction = next(
                (
                    (subject, correction)
                    for left, right, subject, correction in clauses
                    if left <= start < right
                ),
                ("ambiguous", False),
            )
            mentions.append(
                {
                    **mention,
                    'version_component': component,
                    "subject": subject,
                    "event_order": event["order"],
                    "author_id": event["sender_id"],
                    "author_role": event["role"],
                    "explicit_correction": correction,
                    "source": {
                        **mention["source"],
                        "kind": "context_event",
                        "event_pk": event["event_pk"],
                        "message_id": event["external_id"],
                        "event_digest": event["event_digest"],
                        "subject": subject,
                        "verification": "operator_statement"
                        if event["role"] == "owner"
                        else "caller_statement",
                    },
                }
            )
    fields, scope = {}, {}
    for field in sorted(SCOPE_KEYS):
        indices = [
            i
            for i, item in enumerate(mentions)
            if item["field"] == field and item["subject"] == "case_site"
        ]
        current: list[int] = []
        for index in indices:
            item = mentions[index]
            withdrawing = item["status"] == "uncertain" and item["explicit_correction"]
            if item["status"] not in {"affirmed", "negated"} and not withdrawing:
                continue
            # Only explicit newer corrections supersede prior statements; a
            # second author's unexplained different board remains a conflict.
            if item["explicit_correction"]:
                current = [
                    old
                    for old in current
                    if not (
                        mentions[old]["event_order"] < item["event_order"]
                        and (
                            mentions[old]["author_id"] == item["author_id"]
                            or item["author_role"] == "owner"
                        )
                    )
                ]
            current.append(index)
        positive = {
            mentions[i]["value"] for i in current if mentions[i]["status"] == "affirmed"
        }
        negative = {
            mentions[i]["value"] for i in current if mentions[i]["status"] == "negated"
        }
        uncertain = any(mentions[i]["status"] == "uncertain" for i in current)
        state = (
            "conflict"
            if len(positive) > 1 or positive & negative or (positive and uncertain)
            else "known"
            if positive
            else "unknown"
        )
        value = next(iter(positive)) if state == "known" else None
        if value is not None:
            scope[field] = value
        fields[field] = {
            "state": state,
            "value": value,
            "excluded_values": sorted(negative),
            "mention_indexes": indices,
            "current_mention_indexes": current,
        }
    return {
        "schema_version": 1,
        "policy": CONTEXT_FACT_POLICY,
        "query_digest": digest(query),
        "supplied_digest": digest(None),
        "mentions": mentions,
        "fields": fields,
        "observed_scope": scope,
    }
