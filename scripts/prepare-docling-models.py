"""Explicit online provisioning; never invoked while converting attachments.

Run in the locked PDF environment. The destination must be a new private
directory. Review the resulting manifest digest before configuring conversion.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from k3_support.docling_models import MANIFEST, inventory
from k3_support.ids import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Provision public upstream models only. Never attach an implicit owner token.
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    import httpx
    from docling.utils.model_downloader import download_models
    from huggingface_hub import set_client_factory

    # Select the configured proxy explicitly for this provisioning process.
    # No system routing is modified.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    def bounded_request(request):
        print(f"Fetching {request.url.host}{request.url.path}", file=sys.stderr, flush=True)
        request.extensions["timeout"] = {
            "connect": 15,
            "read": 60,
            "write": 60,
            "pool": 15,
        }

    set_client_factory(
        lambda: httpx.Client(
            proxy=proxy,
            follow_redirects=True,
            timeout=60,
            event_hooks={"request": [bounded_request]},
        )
    )

    download_models(
        output_dir=args.output,
        with_layout=True,
        with_tableformer=True,
        with_code_formula=False,
        with_picture_classifier=False,
        with_rapidocr=True,
        rapidocr_models=["onnxruntime:ch"],
        progress=False,
    )
    manifest = inventory(args.output)
    with (args.output / MANIFEST).open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
    print(
        json.dumps(
            {
                "manifest_digest": digest(manifest),
                "files": len(manifest["files"]),
                "note": "Integrity identity only; model origin/license review remains separate.",
            }
        )
    )


if __name__ == "__main__":
    main()
