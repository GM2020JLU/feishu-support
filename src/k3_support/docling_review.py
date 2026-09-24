"""Bounded, inert review view; supplied status/receipt claims confer no trust."""

import json

from .docling_evidence import import_document


def preview(evidence: dict, *, page: int = 1) -> dict:
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "k3-docling-derived-evidence-v1"
    ):
        raise ValueError("请选择附件解析结果 JSON，而不是原始附件")
    if type(page) is not int or not 1 <= page <= 10000:
        raise ValueError("页码不合法")
    source, parser = evidence.get("source"), evidence.get("parser")
    if not isinstance(source, dict) or not isinstance(parser, dict):
        raise ValueError("缺少来源或解析版本")  # noqa: TRY004 -- external JSON validation
    checked = import_document(
        json.dumps(evidence.get("document"), allow_nan=False).encode(),
        source_id=source.get("id"),
        source_version=source.get("version"),
        source_sha256=source.get("sha256"),
        parser_version=parser.get("version"),
        model_revision=parser.get("model_revision"),
    )
    refs = checked["reading_order"]["body"] + checked["reading_order"]["furniture"]
    page_count = max(1, (len(refs) + 19) // 20)
    if page > page_count:
        raise ValueError("页码超出范围")
    items = []
    for ref in refs[(page - 1) * 20 : page * 20]:
        _, collection, index = ref.split("/")
        item = checked["document"][collection][int(index)]
        text = item.get("text", "")
        if not isinstance(text, str):
            raise ValueError("解析正文格式不合法")  # noqa: TRY004 -- external JSON validation
        data = item.get("data", {})
        if collection == "tables" and not isinstance(data, dict):
            raise ValueError("表格格式不合法")
        cells = data.get("table_cells", []) if collection == "tables" else []
        if not isinstance(cells, list) or any(not isinstance(c, dict) for c in cells):
            raise ValueError("表格单元格格式不合法")
        provenance = item.get("prov", [])
        provenance_text = json.dumps(provenance, ensure_ascii=False)
        items.append(
            {
                "reference": ref,
                "kind": collection,
                "text": text[:16000],
                "text_truncated": len(text) > 16000,
                "cells": [
                    {
                        "text": str(c.get("text", ""))[:4000],
                        "text_truncated": len(str(c.get("text", ""))) > 4000,
                        "row": c.get("start_row_offset_idx"),
                        "column": c.get("start_col_offset_idx"),
                        "row_span": c.get("row_span"),
                        "col_span": c.get("col_span"),
                    }
                    for c in cells[:100]
                ],
                "cell_count": len(cells),
                "cells_truncated": len(cells) > 100,
                "provenance": provenance_text[:8000],
                "provenance_truncated": len(provenance_text) > 8000,
            }
        )
    return {
        "read_only": True,
        "status": "unreviewed",
        "source": checked["source"],
        "parser": checked["parser"],
        "page": page,
        "page_count": page_count,
        "items": items,
        "notice": "仅预览，不保存、不批准、不发布；文件中的状态和验证声明不作为审核凭据。",
    }
