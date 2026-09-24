"""Synthetic R3 control-flow evidence; not a semantic-quality Gold set."""

from __future__ import annotations

import json

import pytest
from test_knowledge_runtime import ask, choose_first, professional

from k3_support.ids import canonical_json, digest
from k3_support.knowledge_runtime import load_approved_entry
from k3_support.scope_facts import analyze_scope_facts


@pytest.mark.parametrize(
    ("query", "expected", "absent"),
    [
        ("不是Pico，是EVB，目前还没进入U-Boot", {"board": "evb"}, {"boot_stage"}),
        ("Not Pico, it is EVB. We have not entered U-Boot", {"board": "evb"}, {"boot_stage"}),
        ("以前用eMMC，现在用UFS", {"storage_medium": "ufs"}, set()),
        ("Previously eMMC, now using UFS", {"storage_medium": "ufs"}, set()),
        ("如果换成NVMe，需要什么配置", {}, {"storage_medium"}),
        ("If using NVMe, how would boot work?", {}, {"storage_medium"}),
        ("可能是Pico，板型还没确定", {}, {"board"}),
        ("Is the board Pico?", {}, {"board"}),
        ("Pico还是EVB我不确定", {}, {"board"}),
        ("文档里写着‘在U-Boot里访问UFS’", {}, {"boot_stage", "storage_medium"}),
        ("> Pico U-Boot UFS\n当前是EVB", {"board": "evb"}, {"boot_stage", "storage_medium"}),
        ("Pico和EVB有什么区别", {}, {"board"}),
        ("如果用NVMe，在U-Boot里怎么配置", {}, {"storage_medium", "boot_stage"}),
        ("以前用Pico，UFS能启动", {}, {"board", "storage_medium"}),
        ("资料里提到Pico，以及UFS配置", {}, {"board", "storage_medium"}),
        ("无法进入U-Boot", {}, {"boot_stage"}),
        ("UFS没有接，使用eMMC", {"storage_medium": "emmc"}, set()),
        ("不用Pico，用EVB", {"board": "evb"}, set()),
        ("非Pico的风扇", {}, {"board"}),
        ("Pico or another board", {}, {"board"}),
        ("不是不使用Pico", {}, {"board"}),
        ("只是提到U-Boot", {}, {"boot_stage"}),
        ("怎么进入U-Boot", {}, {"boot_stage"}),
        ("How do I enter U-Boot?", {}, {"boot_stage"}),
        ("建议使用NVMe", {}, {"storage_medium"}),
        ("版本不是commit-v1", {}, {"software_version"}),
        ("板子是Pico报错字符串U-Boot", {"board": "k3-pico-itx"}, {"boot_stage"}),
        ("当前阶段是U-Boot", {"boot_stage": "u-boot"}, set()),
        ("pico怎么在Uboot里查看UFS设备", {"board": "k3-pico-itx", "boot_stage": "u-boot", "storage_medium": "ufs"}, set()),
    ],
)
def test_mentions_are_not_all_current_facts(query, expected, absent):
    report = analyze_scope_facts(query)
    assert report["observed_scope"] == expected
    assert not (set(report["observed_scope"]) & absent)
    assert report["schema_version"] == 1
    for mention in report["mentions"]:
        source = mention["source"]
        assert source["input_digest"] == digest(query)
        assert query[source["start"] : source["end"]] == mention["text"]


def test_conflicting_supplied_scope_is_not_a_silent_override():
    report = analyze_scope_facts("不是Pico，是EVB", {"board": "k3-pico-itx"})
    assert "board" not in report["observed_scope"]
    assert report["fields"]["board"]["state"] == "conflict"
    assert {item["source"]["kind"] for item in report["mentions"]} == {"query", "supplied"}


def test_two_current_objects_are_not_merged_into_one_environment():
    report = analyze_scope_facts("测试板是Pico，现场是EVB")
    assert report["fields"]["board"]["state"] == "conflict"
    assert report["observed_scope"] == {}


def test_explicit_version_history_and_current_observation():
    report = analyze_scope_facts("之前版本是commit-v1，现在版本是commit-v2")
    assert report["observed_scope"] == {"software_version": "commit-v2"}
    assert [item["status"] for item in report["mentions"]] == ["historical", "affirmed"]


def test_wrong_board_is_filtered_before_actual_selector(conn):
    key = professional(conn, kind="document_route")

    def never_called(*_):
        pytest.fail("known wrong board reached selector")

    result = ask(conn, "不是Pico，是EVB，风扇怎么设置", selector=never_called)
    assert result["selected_entry"] is None
    assert result["candidates"] == []
    assert result["trace"]["filter_counts"] == {"scope_mismatch": 1}
    assert load_approved_entry(conn, knowledge_id=key, requester_id="ou_peer", chat_id="oc_peer", query="不是Pico，是EVB，风扇") is None


def test_negative_only_scope_cannot_take_link_only_shortcut(conn):
    key = professional(conn, kind="document_route")
    result = ask(conn, "不是Pico，风扇怎么设置", selector=choose_first)
    assert result["selected_entry"] is None
    assert load_approved_entry(conn, knowledge_id=key, requester_id="ou_peer", chat_id="oc_peer", query="不是Pico，风扇") is None


def test_hypothetical_version_does_not_satisfy_command_prerequisite(conn):
    professional(conn)
    result = ask(conn, "Pico风扇，如果版本是commit-v1怎么操作", observed_scope={"boot_stage": "userspace"}, selector=choose_first)
    assert result["selected_entry"] is None
    assert result["abstention_reason"] == "version_unknown"
    assert "software_version" not in result["scope"]


def test_supplied_fact_conflict_blocks_query_and_final_load(conn):
    key = professional(conn)
    supplied = {"board": "k3-pico-itx", "boot_stage": "userspace", "software_version": "commit-v1"}
    query = "不是Pico，是EVB，风扇 pwm1_enable"
    result = ask(conn, query, observed_scope=supplied, selector=choose_first)
    assert result["selected_entry"] is None
    assert result["abstention_reason"] == "ambiguous_scope"
    assert result["scope_facts"]["fields"]["board"]["state"] == "conflict"
    assert load_approved_entry(conn, knowledge_id=key, requester_id="ou_peer", chat_id="oc_peer", query=query, observed_scope=supplied) is None


def test_unknown_scope_is_not_filled_from_candidate_and_provenance_is_bound(conn):
    key = professional(conn, kind="document_route")
    result = ask(conn, "风扇", selector=choose_first)
    assert result["selected_knowledge_id"] == key
    assert result["scope"] == {}
    assert result["scope_facts"]["mentions"] == []
    assert result["selected_entry"]["knowledge_scope_facts"] == result["scope_facts"]


def test_new_fact_policy_changes_runtime_release_contract(conn):
    result = ask(conn, "Pico 风扇")
    assert result["runtime_binding"]["scope_policy"] == result["scope_facts"]["policy"]
    assert result["runtime_binding"]["scope_policy"] != "observed-only-acl-first-v1"


@pytest.mark.parametrize("supplied", [{"expected_board": "Pico"}, {"board": ""}, {"board": True}, ["Pico"]])
def test_invalid_caller_observations_cannot_become_facts(supplied):
    with pytest.raises(ValueError):
        analyze_scope_facts("风扇", supplied)


def test_caller_observation_is_explicitly_not_independently_verified():
    report = analyze_scope_facts("风扇", {"software_version": "commit-v1"})
    mention = report["mentions"][0]
    assert mention["reason"] == "legacy_caller_observation"
    assert mention["source"]["verification"] == "caller_assertion"


@pytest.mark.parametrize(
    "correction",
    ["版本不是commit-v1", "尚未进入userspace", "不是Pico"],
)
def test_explicit_negative_of_a_caller_assertion_blocks_actual_command(conn, correction):
    key = professional(conn)
    scope = {"board": "k3-pico-itx", "software_version": "commit-v1", "boot_stage": "userspace"}
    query = f"风扇 pwm1_enable，{correction}"
    result = ask(conn, query, observed_scope=scope, selector=choose_first)
    assert result["selected_entry"] is None
    assert load_approved_entry(conn, knowledge_id=key, requester_id="ou_peer", chat_id="oc_peer", query=query, observed_scope=scope) is None


def test_instruction_target_does_not_claim_physical_stage_was_reached():
    report = analyze_scope_facts("在U-Boot里查看UFS")
    stage = next(mention for mention in report["mentions"] if mention["field"] == "boot_stage")
    assert stage["status"] == "affirmed"
    assert stage["role"] == "target"
    assert stage["source"]["verification"] == "caller_statement"


@pytest.mark.parametrize("failure", [
    "不能进入 U-Boot", "不能正常进入 U-Boot", "未到 U-Boot", "上不了 U-Boot",
    "进不去 U-Boot", "没能启动到 U-Boot", "无法到达 U-Boot", "U-Boot 进不去",
    "failed to enter U-Boot", "unable to reach U-Boot", "could not boot into U-Boot",
    "U-Boot cannot be reached",
])
@pytest.mark.parametrize("old_stage", [False, True])
def test_failed_stage_transition_cannot_satisfy_actual_query_or_load(conn, failure, old_stage):
    key = professional(conn)
    row = conn.execute("SELECT revision_id,payload_json FROM professional_knowledge_revisions WHERE knowledge_id=?", (key,)).fetchone()
    metadata = json.loads(row["payload_json"])
    metadata["scope"]["boot_stages"] = ["u-boot"]
    conn.execute("UPDATE professional_knowledge_revisions SET payload_json=? WHERE revision_id=?", (canonical_json(metadata), row["revision_id"]))
    supplied = {"software_version": "commit-v1"}
    if old_stage:
        supplied["boot_stage"] = "u-boot"
    query = f"K3 Pico {failure}，风扇 pwm1_enable"
    result = ask(conn, query, observed_scope=supplied, selector=choose_first)
    stage = result["scope_facts"]["fields"]["boot_stage"]
    assert "u-boot" in stage["excluded_values"]
    assert stage["state"] == ("conflict" if old_stage else "unknown")
    assert "boot_stage" not in result["scope"]
    assert result["selected_entry"] is None
    assert load_approved_entry(conn, knowledge_id=key, requester_id="ou_peer", chat_id="oc_peer", query=query, observed_scope=supplied) is None
