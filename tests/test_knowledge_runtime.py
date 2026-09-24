"""Synthetic retrieval/authority tests. These are not human-reviewed Gold."""

from __future__ import annotations

import pytest

from k3_support.ids import canonical_json, digest
from k3_support.knowledge import create_candidate, review, semantic_match
from k3_support.knowledge_runtime import (
    RetrievalError,
    corpus_fingerprint,
    corpus_rows,
    query_knowledge,
    query_tokens,
    retrieval_options,
)
from k3_support.routing import set_requester_profile


def entry(conn, title="Pico 风扇配置", *, disclosure="public", version=None):
    key = create_candidate(
        conn,
        title=title,
        questions=[title],
        answer_markdown=f"Synthetic answer: {title}",
        project="K3",
        module="thermal",
        software_version=version,
        disclosure_class=disclosure,
        confidence=0.99,
        source_authority=0.99,
        canonical_case_id=None,
        source_digest=digest([title, disclosure, version]),
    )
    review(
        conn, knowledge_id=key, reviewer_id="synthetic-reviewer", decision="approved"
    )
    return key


def professional(
    conn, *, kind="command_reference", board="k3-pico-itx", version="commit-v1"
):
    key = entry(conn, f"Pico 风扇 pwm1_enable {kind}", version=version)
    metadata = {
        "id": "fixture.fan",
        "kind": kind,
        "scope": {
            "product": "K3",
            "component": "thermal",
            "basis": "hardware_specific",
            "boards": [board],
            "software_versions": [version],
            "boot_stages": ["userspace"],
        },
        "intent": {
            "required_entities": [],
            "negative_constraints": ["not for EC firmware flashing"],
        },
        "sources": [
            {"share_mode": "link_only" if kind == "document_route" else "full_answer"}
        ],
        "claims": [{"id": "fan-synthetic"}],
    }
    rev = "rev_" + key
    conn.execute(
        """INSERT INTO professional_knowledge_revisions
        (revision_id,stable_id,revision_number,knowledge_id,kind,lifecycle_state,revision_digest,
         payload_json,body_markdown,owner,reviewed_by,reviewed_at,review_due_at,imported_at)
        VALUES(?,?,1,?,?,'published',?,?,'synthetic','fixture','fixture',
               '2026-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
        (rev, rev, key, kind, digest(metadata), canonical_json(metadata)),
    )
    conn.execute(
        "UPDATE knowledge_entries SET professional_revision_id=? WHERE knowledge_id=?",
        (rev, key),
    )
    return key


def ask(conn, query="风扇满转好吵，想让它安静一点", **kwargs):
    # Synthetic setup performs the explicit build stage; production query must
    # never silently do this. Unbuilt/stale-query contracts use direct calls.
    from k3_support.knowledge_corpus import build
    assert build(conn)['built']
    return query_knowledge(
        conn, query=query, requester_id="ou_peer", chat_id="oc_peer", **kwargs
    )


def choose_first(query, catalog):
    return {"knowledge_id": catalog[0]["knowledge_id"], "confidence": 0.99}


@pytest.mark.parametrize('backend', ['sqlite', 'qdrant'])
@pytest.mark.parametrize('query,title', [
    ('今天食堂有什么菜', '环境里有什么变量'),
    ('帮我批准这个采购订单', '这个命令的用法'),
    ('你保证客户今晚一定能验收通过吗', '启动检查通过后继续'),
])
def test_lexical_overlap_never_becomes_answer_without_selector(conn, backend, query, title):
    key = entry(conn, title)
    result = ask(conn, query, options={'backend': backend})
    assert key in result['retrieved_knowledge_ids']
    assert result['selected_entry'] is None and result['selected_knowledge_id'] is None
    assert result['abstention_reason'] == 'semantic_selection_required'
    assert result['runtime_binding']['selection']['provider'] == 'none'


def test_fts_membership_query_preserves_rank_and_acl_filter(conn):
    from k3_support.knowledge_runtime import _fts_ranks

    entry(conn, "UFS storage secret", disclosure="restricted")
    allowed = [entry(conn, f"UFS storage public {index}") for index in range(12)]
    entry(conn, "unrelated public")
    rows = [row for row in corpus_rows(conn) if row["knowledge_id"] in allowed]
    expected = {
        row["knowledge_id"]: rank
        for rank, row in enumerate(conn.execute(
            """SELECT knowledge_fts.knowledge_id,bm25(knowledge_fts) AS rank
            FROM knowledge_fts JOIN json_each(?) eligible ON eligible.value=knowledge_fts.knowledge_id
            WHERE knowledge_fts MATCH ? ORDER BY rank,knowledge_fts.knowledge_id""",
            (canonical_json(allowed), '"ufs" OR "storage"'),
        ))
    }
    assert _fts_ranks(conn, rows, "ufs storage") == expected
    assert set(expected) == set(allowed)
    assert _fts_ranks(conn, [], "ufs storage") == {}


def test_old_relevant_article_beyond_latest_100_is_recalled(conn):
    wanted = entry(conn)
    conn.execute(
        "UPDATE knowledge_entries SET updated_at='2001-01-01' WHERE knowledge_id=?",
        (wanted,),
    )
    for number in range(151):
        entry(conn, f"unrelated document {number}")
    result = ask(conn, selector=choose_first)
    assert result["retrieved_knowledge_ids"] == [wanted]
    assert result["selected_knowledge_id"] == wanted
    assert "answer_markdown" not in result["candidates"][0]


@pytest.mark.parametrize("matches", [True, False])
def test_selection_observation_is_scoped_to_exact_returned_result(conn, matches):
    from k3_support.semantic import observed_inference

    entry(conn)
    old = {"result_digest": "previous-call"}
    token = observed_inference.set(old)
    def select(query, catalog):
        result = choose_first(query, catalog)
        observed_inference.set({"result_digest": digest(result) if matches else "other-result",
                                "verification": "bridge_sdk_observation", "model": "fixture-model",
                                "provider": "fixture-provider", "manifest_digest": "a" * 64})
        return result
    try:
        result = ask(conn, selector=select)
        assert ("selection_observation" in result["trace"]) is matches
        assert ("knowledge_selection_observation" in result["selected_entry"]) is matches
        assert observed_inference.get() is old
        if matches:
            binding = result["runtime_binding"]["selection"]
            assert binding["model"] == "fixture-model"
            assert binding["verification"] == "bridge_sdk_observation"
            assert "api_request_id" not in binding  # per-call data is not runtime identity
            from k3_support.knowledge_release import _runtime_ready
            assert _runtime_ready(result["runtime_binding"])
        else:
            assert result["runtime_binding"]["selection"]["verification"] == "unverified"
        assert "selection_observation" not in ask(conn, selector=choose_first)["trace"]
    finally:
        observed_inference.reset(token)


def test_acl_before_candidate_limit_and_model(conn):
    for number in range(120):
        entry(conn, f"风扇 pwm1_enable 秘密 {number}", disclosure="restricted")
    wanted = entry(conn, "风扇 public")
    internal = entry(conn, "风扇 internal", disclosure="internal")
    seen = []

    def select(query, catalog):
        seen.extend(item["knowledge_id"] for item in catalog)
        return choose_first(query, catalog)

    assert ask(conn, selector=select)["selected_knowledge_id"] == wanted
    assert seen == [wanted]
    assert internal not in seen
    set_requester_profile(
        conn,
        requester_id="ou_peer",
        relationship="external",
        function_role="engineering",
        source="operator",
        relationship_confidence=1,
        function_confidence=1,
    )
    assert ask(conn)["retrieved_knowledge_ids"] == [wanted]
    set_requester_profile(
        conn,
        requester_id="ou_peer",
        relationship="peer",
        function_role="engineering",
        source="operator",
        relationship_confidence=1,
        function_confidence=1,
    )
    assert set(ask(conn)["retrieved_knowledge_ids"]) == {wanted, internal}


def test_exact_error_command_and_cjk_tokens(conn):
    assert {"ufs_init", "-110", "pwm1_enable", "0x3a", "bootcmd", "风扇"} <= set(
        query_tokens("ufs_init -110 pwm1_enable 0x3a bootcmd 风扇太吵了")
    )
    wanted = entry(conn, "ufs_init returned -110")
    entry(conn, "ufs init returned -111")
    result = ask(conn, "ufs_init -110")
    assert result['retrieved_knowledge_ids'][0] == wanted
    assert result['selected_knowledge_id'] is None
    assert result['abstention_reason'] == 'semantic_selection_required'


def test_scope_literals_next_to_chinese_are_observations_not_catalog_guesses(conn):
    result = ask(conn, "pico怎么在Uboot里查看UFS设备")
    assert result["scope"] == {
        "board": "k3-pico-itx",
        "boot_stage": "u-boot",
        "storage_medium": "ufs",
    }


@pytest.mark.parametrize(
    "scope",
    [
        {"board": "board1"},
        {"software_version": "commit-v2"},
        {"boot_stage": "u-boot"},
    ],
)
def test_wrong_scope_never_reaches_model(conn, scope):
    professional(conn)

    def no_model(*_):
        pytest.fail("wrong-scope metadata reached selector")

    result = ask(conn, "风扇", selector=no_model, observed_scope=scope)
    assert not result["candidates"]


def test_unknown_command_scope_abstains_but_link_does_not_overask(conn):
    key = professional(conn)
    result = ask(conn, "pico 风扇 pwm1_enable", selector=choose_first)
    assert result["abstention_reason"] == "version_unknown"
    known = ask(
        conn,
        "pico 风扇 pwm1_enable",
        observed_scope={"software_version": "commit-v1", "boot_stage": "userspace"},
        selector=choose_first,
    )
    assert known["selected_knowledge_id"] == key
    assert known["selected_entry"]["knowledge_claim_ids"] == ["fan-synthetic"]
    conn.execute(
        "UPDATE knowledge_entries SET status='retired' WHERE knowledge_id=?", (key,)
    )
    link = professional(conn, kind="document_route")
    assert ask(conn, "风扇", selector=choose_first)["selected_knowledge_id"] == link
    assert ask(conn, "风扇")["scope"] == {}  # No environment borrowed from article.


@pytest.mark.parametrize("change", ["body", "acl", "status", "due", "profile"])
def test_late_selector_cannot_return_changed_or_revoked_entry(conn, change):
    key = entry(conn, disclosure="internal" if change == "profile" else "public")
    set_requester_profile(
        conn,
        requester_id="ou_peer",
        relationship="peer",
        function_role="engineering",
        source="operator",
        relationship_confidence=1,
        function_confidence=1,
    )

    def selector(query, catalog):
        assert catalog
        assert not conn.in_transaction
        if change == "profile":
            conn.execute(
                "UPDATE requester_profiles SET relationship='external' WHERE requester_id='ou_peer'"
            )
        else:
            statement = {
                "body": "answer_markdown='silently changed without digest update'",
                "acl": "disclosure_class='restricted'",
                "status": "status='retired'",
                "due": "review_due_at='2000-01-01T00:00:00+00:00'",
            }[change]
            conn.execute(
                f"UPDATE knowledge_entries SET {statement} WHERE knowledge_id=?", (key,)
            )
        return {"knowledge_id": key, "confidence": 0.99}

    assert ask(conn, selector=selector)["selected_entry"] is None


def test_selector_failure_never_becomes_lexical_answer(conn):
    entry(conn)
    assert ask(conn, selector=lambda *_: None)["selected_entry"] is None
    assert (
        ask(conn, selector=lambda *_: {"knowledge_id": "invented", "confidence": 1})[
            "selected_entry"
        ]
        is None
    )
    assert (
        ask(
            conn,
            selector=lambda *_: {
                "knowledge_id": "invented",
                "confidence": float("nan"),
            },
        )["selected_entry"]
        is None
    )


def test_fingerprint_ignores_usage_but_not_contents_or_permission(conn):
    key = entry(conn)
    before = corpus_fingerprint(corpus_rows(conn))
    first = ask(conn, selector=choose_first)
    conn.execute(
        "UPDATE knowledge_entries SET use_count=use_count+1,updated_at='changed usage' WHERE knowledge_id=?",
        (key,),
    )
    assert corpus_fingerprint(corpus_rows(conn)) == before
    second = ask(conn, "风扇", selector=choose_first)
    assert first["runtime_binding"] == second["runtime_binding"]
    assert (
        first["selected_entry"]["knowledge_query_digest"]
        != second["selected_entry"]["knowledge_query_digest"]
    )
    conn.execute(
        "UPDATE knowledge_entries SET allowed_user_ids_json='[\"new-user\"]' WHERE knowledge_id=?",
        (key,),
    )
    assert corpus_fingerprint(corpus_rows(conn)) != before


def test_fallback_changes_binding_and_can_be_disabled(conn):
    entry(conn)
    baseline = ask(conn)
    fallback = ask(conn, options={"backend": "qdrant"})
    assert fallback["trace"]["fallback"] == "RetrievalError"
    assert fallback["runtime_binding"]["effective_backend"] == "sqlite"
    assert baseline["runtime_binding"] != fallback["runtime_binding"]
    unavailable = ask(conn, options={"backend": "qdrant", "allow_fallback": False})
    assert unavailable["selected_entry"] is None
    assert unavailable["runtime_binding"]["effective_backend"] == "unavailable"


def test_no_gold_inputs_and_shared_semantic_entry(conn):
    key = entry(conn)
    with pytest.raises(TypeError):
        ask(conn, expected_route="direct_answer")
    selected = semantic_match(
        conn,
        query="风扇",
        requester_id="ou_peer",
        chat_id="oc_peer",
        selector=choose_first,
    )
    direct = ask(conn, "风扇", selector=choose_first)
    assert selected == direct["selected_entry"]
    assert selected["knowledge_id"] == key
    assert (
        selected["knowledge_runtime_binding"]["selection"]["verification"]
        == "unverified"
    )


@pytest.mark.parametrize(
    "options",
    [
        {"candidate_limit": True},
        {"prefetch_limit": 2},
        {"qdrant_path": "/tmp/../private"},
        {"gold": []},
    ],
)
def test_options_fail_closed(options):
    with pytest.raises(RetrievalError):
        retrieval_options(options)


def test_profile_expiry_is_checked_even_when_old_snapshot_is_supplied(conn):
    entry(conn, disclosure="internal")
    profile = set_requester_profile(
        conn,
        requester_id="ou_peer",
        relationship="peer",
        function_role="engineering",
        source="feishu_contact",
        relationship_confidence=1,
        function_confidence=1,
        verified_at="2026-01-01T00:00:00+00:00",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert ask(conn, verified_profile=profile)["candidates"] == []


def test_unmapped_required_prerequisite_is_not_invented(conn):
    import json

    key = professional(conn)
    row = conn.execute(
        "SELECT revision_id,payload_json FROM professional_knowledge_revisions WHERE knowledge_id=?",
        (key,),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    payload["intent"]["required_entities"] = ["known stable supply voltage"]
    conn.execute(
        "UPDATE professional_knowledge_revisions SET payload_json=? WHERE revision_id=?",
        (canonical_json(payload), row["revision_id"]),
    )
    assert (
        ask(
            conn,
            "pico 风扇",
            observed_scope={"software_version": "commit-v1", "boot_stage": "userspace"},
            selector=choose_first,
        )["abstention_reason"]
        == "ambiguous_scope"
    )
