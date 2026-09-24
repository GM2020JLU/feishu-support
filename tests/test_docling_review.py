import pytest

from k3_support.docling_review import preview


def evidence():
    return {
        "schema": "k3-docling-derived-evidence-v1",
        "status": "approved",
        "source": {"id": "synthetic", "version": "1", "sha256": "a" * 64},
        "parser": {
            "version": "fixture",
            "model_revision": "not-used",
            "metadata_verified": True,
        },
        "document": {
            "schema_name": "DoclingDocument",
            "version": "1.7.0",
            "body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/0"}]},
            "furniture": {"self_ref": "#/furniture"},
            "texts": [{"self_ref": "#/texts/0", "text": "<script>untrusted</script>"}],
        },
    }


def test_preview_does_not_trust_approval_or_receipts():
    result = preview(evidence())
    assert result["status"] == "unreviewed"
    assert result["read_only"] is True
    assert result["parser"]["metadata_verified"] is False
    assert result["items"][0]["text"] == "<script>untrusted</script>"


@pytest.mark.parametrize("page", [True, 0, -1, "1", 2])
def test_invalid_page(page):
    with pytest.raises(ValueError):
        preview(evidence(), page=page)


def test_preview_labels_each_truncation_without_modifying_source():
    import copy
    value = evidence()
    doc = value['document']
    doc['body']['children'] = [{'$ref': '#/tables/0'}]
    doc['texts'] = []
    doc['tables'] = [{'self_ref': '#/tables/0', 'prov': [{'note': 'p' * 8000}],
        'data': {'table_cells': [{'text': 'x' * 4001, 'start_row_offset_idx': i,
                                 'start_col_offset_idx': 0} for i in range(101)]}}]
    before = copy.deepcopy(value)
    item = preview(value)['items'][0]
    assert item['cell_count'] == 101 and len(item['cells']) == 100
    assert item['cells_truncated'] and item['provenance_truncated']
    assert len(item['provenance']) == 8000
    assert all(cell['text_truncated'] and len(cell['text']) == 4000 for cell in item['cells'])
    assert value == before
