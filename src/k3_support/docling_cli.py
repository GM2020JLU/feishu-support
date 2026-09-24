"""Explicit local attachment export; no live configuration or knowledge writes."""

import argparse
import json
import math
import os
import stat
import sys
import sysconfig
import tempfile
from pathlib import Path

from .docling_convert import MAX_INPUT_BYTES, convert_document


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_INPUT_BYTES:
            raise ValueError("input must be a bounded regular file")
        content = stream.read(MAX_INPUT_BYTES + 1)
        if not 0 < len(content) <= MAX_INPUT_BYTES:
            raise ValueError("input size changed beyond limit")
        return content


def export(args) -> dict:
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError("output already exists")
    content = _read(args.input)
    model_options = (
        {"models": args.models, "model_digest": args.model_digest}
        if args.models is not None or args.model_digest is not None
        else {}
    )
    evidence = convert_document(
        content,
        format=args.input.suffix.lower().lstrip("."),
        source_id=args.source_id,
        source_version=args.source_version,
        site_packages=args.site_packages,
        expected_version=args.parser_version,
        timeout=args.timeout,
        **model_options,
    )
    payload = json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode()
    # Same-directory publication is atomic and cannot replace an existing name.
    fd, name = tempfile.mkstemp(prefix=".attachment-pending-", dir=args.output.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(staged, args.output)
    finally:
        staged.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "unreviewed",
        "bytes": len(payload),
        "source_hash_verified": evidence["conversion"]["source_hash_verified"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a local Office attachment to unreviewed JSON; no publication."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New JSON file in an existing directory; never overwritten.",
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--parser-version", required=True)
    parser.add_argument(
        "--site-packages", type=Path, default=Path(sysconfig.get_paths()["purelib"])
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--models", type=Path, help="Explicit offline PDF/image model bundle directory."
    )
    parser.add_argument(
        "--model-digest", help="Expected model manifest SHA-256 identity."
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 600:
        parser.error("timeout must be finite and in (0, 600]")
    try:
        print(json.dumps(export(args)))
        return 0
    except Exception as exc:  # noqa: BLE001 -- do not disclose document contents or parser stderr
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "note": "No automatic retry. Check whether the output exists before retrying.",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
