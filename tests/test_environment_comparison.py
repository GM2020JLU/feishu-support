from k3_support.context_facts import project_facts
from k3_support.environment_comparison import compare_statements, comparison_lines
from k3_support.environment_comparison import compare_version_observations


def facts(*texts):
    return project_facts(
        [
            {
                "content": text,
                "sender_id": "p",
                "role": "requester",
                "order": i,
                "event_pk": f"e{i}",
                "external_id": f"m{i}",
                "event_digest": f"digest{i}",
            }
            for i, text in enumerate(texts)
        ],
        query="\n".join(texts),
        requester_id="p",
    )


def test_component_bound_version_comparison_does_not_cross_components():
    observed = dict(component='u-boot', version='2025.01', multiple_versions=False,
                    observed_at='2026-09-09', evidence_id='fixture')
    for text, expected in [('当前 U-Boot 版本是 2025.01','same_version_text'),
                           ('当前 U-Boot 版本是 2024.01','different_version_text'),
                           ('当前 Linux 版本是 2025.01','unknown'),
                           ('当前版本是 2025.01','unknown'),
                           ('当前 U-Boot 和 Linux 版本是 2025.01','unknown')]:
        assert compare_version_observations(facts(text), [observed])[0]['comparison'] == expected
    observed['multiple_versions'] = True
    assert compare_version_observations(facts('当前 U-Boot 版本是 2025.01'), [observed])[0]['comparison'] == 'conflict'


def test_spl_component_and_old_fact_projection_are_not_relabelled():
    data = facts('当前 U-Boot SPL 版本是 2025.01')
    versions = [m for m in data['mentions'] if m['field'] == 'software_version']
    assert versions[0]['version_component'] == 'spl'
    data['policy'] = 'conversation-facts-v2'
    assert compare_version_observations(data, []) == []


def field(data, name):
    return next(row for row in compare_statements(data) if row["field"] == name)


def test_sourced_statements_compare_without_claiming_measurement():
    row = field(facts("当前使用 UFS", "board1 当前使用 NVMe"), "storage_medium")
    assert row["comparison"] == "different_statement"
    assert row["site"]["value"] == "ufs"
    assert row["board1"]["value"] == "nvme"
    assert row["site"]["sources"][0]["message_id"] == "m0"
    assert row["board1"]["sources"][0]["verification"] == "caller_statement"


def test_targets_and_quoted_examples_are_not_observations():
    row = field(facts("UFS怎么用", "board1 文档示例：使用 UFS"), "storage_medium")
    assert row["comparison"] == "unknown"


def test_site_correction_and_unresolved_board_conflict():
    data = facts(
        "当前使用 UFS", "现在改为 NVMe", "board1 当前使用 UFS", "board1 当前使用 NVMe"
    )
    row = field(data, "storage_medium")
    assert row["site"]["value"] == "nvme"
    assert row["board1"]["state"] == "conflict"
    assert row["comparison"] == "conflict"


def test_same_statement_is_not_verified_success():
    row = field(facts("当前使用 UFS", "board1 当前使用 UFS"), "storage_medium")
    assert row["comparison"] == "same_statement"
    assert "resolved" not in row and "verified" not in row


def test_missing_context_is_read_only_and_unknown(conn):
    before = list(conn.iterdump())
    assert "未知" in comparison_lines(conn, case_id="K3-test", lifecycle_round=1)[-1]
    assert list(conn.iterdump()) == before


def test_mixed_message_preserves_subjects_and_source_offsets():
    text = "现场当前使用 UFS，board1 当前使用 NVMe"
    data = facts(text)
    row = field(data, "storage_medium")
    assert row["comparison"] == "different_statement"
    assert row["site"]["value"] == "ufs"
    assert row["board1"]["value"] == "nvme"
    for mention in data["mentions"]:
        source = mention["source"]
        assert text[source["start"]:source["end"]] == mention["text"]


def test_explicit_site_switch_and_ambiguous_same_clause():
    row = field(facts("board1 当前使用 NVMe，现场当前使用 UFS"), "storage_medium")
    assert row["site"]["value"] == "ufs" and row["board1"]["value"] == "nvme"
    row = field(facts("现场与 board1 当前使用 UFS"), "storage_medium")
    assert row["comparison"] == "unknown"


def test_quoted_board_does_not_reassign_site_and_old_projection_is_unknown():
    data = facts('当前使用 UFS，示例是 "board1"')
    assert data["fields"]["storage_medium"]["value"] == "ufs"
    data["policy"] = "conversation-facts-v1"
    assert all(row["comparison"] == "unknown" for row in compare_statements(data))
