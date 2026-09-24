"""Run real PDF/OCR conversion in the offline sandbox using explicit fixtures."""

import argparse
import json
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from k3_support.docling_convert import convert_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    checks = []
    for name in (
        "native.pdf",
        "scan.pdf",
        "scan.png",
        "scan-table.pdf",
        "scan-table.png",
    ):
        source = args.fixtures / name
        result = convert_document(
            source.read_bytes(),
            format=source.suffix[1:],
            source_id="synthetic:" + name,
            source_version="1",
            expected_version="2.126.0",
            site_packages=Path(sysconfig.get_paths()["purelib"]),
            models=args.models,
            model_digest=args.model_digest,
            timeout=300,
        )
        document = result["document"]
        text = json.dumps(document, ensure_ascii=False)
        for marker in ("PWM", "128", "2400", "风扇"):
            if marker not in text:
                raise ValueError(f"{name}: missing required content {marker}")
        if not any(item.get("prov") for item in document.get("texts", [])):
            raise ValueError(f"{name}: missing source coordinates")
        if name == "native.pdf" and not document.get("tables"):
            raise ValueError("native PDF table structure missing")
        if name == "native.pdf" or name.startswith("scan-table"):
            matching = []
            for table in document.get("tables", []):
                cells = table.get("data", {}).get("table_cells", [])
                rows = {}
                for cell in cells:
                    rows.setdefault(cell.get("start_row_offset_idx"), {})[
                        cell.get("start_col_offset_idx")
                    ] = cell.get("text", "").strip()
                if all(
                    any(
                        row.get(0) == key and row.get(1) == value
                        for row in rows.values()
                    )
                    for key, value in (("PWM", "128"), ("RPM", "2400"))
                ):
                    matching.append(cells)
            if not matching:
                raise ValueError(
                    f"{name}: table parameter/value row associations missing"
                )
            if name.startswith("scan-table") and not any(
                cell.get("col_span") == 2 and "Fan telemetry" in cell.get("text", "")
                for cells in matching
                for cell in cells
            ):
                raise ValueError(f"{name}: merged table header missing")
        if result["status"] != "unreviewed" or result["automatic_reply_eligible"]:
            raise ValueError("synthetic evidence became eligible")
        checks.append(
            {
                "name": name,
                "tables": len(document.get("tables", [])),
                "text_items": len(document.get("texts", [])),
            }
        )
    print(json.dumps({"ok": True, "live_data_used": False, "checks": checks}))


if __name__ == "__main__":
    main()
