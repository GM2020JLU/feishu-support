import json

import pytest

from k3_support.docling_evidence import import_document


def document():
    return {
        "schema_name": "DoclingDocument",
        "version": "1.7.0",
        "body": {
            "self_ref": "#/body",
            "children": [{"$ref": "#/tables/0"}, {"$ref": "#/texts/0"}],
        },
        "furniture": {"self_ref": "#/furniture", "children": []},
        "texts": [{"self_ref": "#/texts/0", "text": "风扇设置", "children": []}],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "children": [],
                "data": {"table_cells": [{"text": "PWM", "row_span": 2}]},
                "prov": [{"page_no": 3, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
            }
        ],
    }


def run(doc):
    return import_document(
        json.dumps(doc).encode(),
        source_id="attachment:example",
        source_version="rev-1",
        source_sha256="a" * 64,
        parser_version="test-fixture",
        model_revision="not-used",
    )


def test_preserves_structure_and_never_approves():
    doc = document()
    result = run(doc)
    assert result["document"] == doc
    assert result["reading_order"]["body"] == ["#/tables/0", "#/texts/0"]
    assert result["status"] == "unreviewed"
    assert result["automatic_reply_eligible"] is False
    assert result["parser"]["metadata_verified"] is False


@pytest.mark.parametrize(
    "target", ["#/missing/0", "https://example.com", "#/body", "#/tables/0"]
)
def test_rejects_invalid_edges_and_cycles(target):
    doc = document()
    doc["texts"][0]["children"] = [{"$ref": target}]
    with pytest.raises(ValueError):
        run(doc)


def test_rejects_orphans():
    doc = document()
    doc["body"]["children"].pop()
    with pytest.raises(ValueError, match="orphan"):
        run(doc)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"\xff", b""])
def test_rejects_invalid_json(raw):
    with pytest.raises(ValueError):
        import_document(
            raw,
            source_id="id",
            source_version="v",
            source_sha256="a" * 64,
            parser_version="fixture",
            model_revision="not-used",
        )
