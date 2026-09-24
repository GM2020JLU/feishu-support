"""Optional networkless Office conversion using the existing process supervisor."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from .broker_process import run_process
from .docling_evidence import import_document
from .replay_sandbox import sandbox_command

MAX_INPUT_BYTES = 32 * 1024 * 1024
OFFICE_FORMATS = {"docx", "pptx", "xlsx"}
MODEL_FORMATS = {"pdf", "png", "jpg", "jpeg", "tiff"}


def _worker() -> None:
    """Only invoked by the fixed bootstrap inside the namespace."""
    import contextlib
    import sys
    from importlib.metadata import PackageNotFoundError, version

    # Optional imports deliberately stay inside the sandbox process.
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.document_converter import DocumentConverter

    request = json.load(sys.stdin)
    try:
        installed = version("docling-slim")
    except PackageNotFoundError:
        installed = version("docling")
    if installed != request["expected_version"]:
        raise ValueError("Docling version differs from deployment selection")
    fmt = request["format"]
    if fmt not in OFFICE_FORMATS | MODEL_FORMATS:
        raise ValueError("unsupported conversion format")
    format_options = {}
    input_format = (
        InputFormat.IMAGE
        if fmt in MODEL_FORMATS - {"pdf"}
        else getattr(InputFormat, fmt.upper())
    )
    if fmt in MODEL_FORMATS:
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            RapidOcrOptions,
        )
        from docling.document_converter import ImageFormatOption, PdfFormatOption

        from .docling_models import verify

        verify(Path("/models"), request["model_digest"])
        options = PdfPipelineOptions(
            artifacts_path="/models",
            enable_remote_services=False,
            allow_external_plugins=False,
            do_ocr=True,
            do_table_structure=True,
            ocr_options=RapidOcrOptions(backend="onnxruntime", lang=["ch"]),
            accelerator_options=AcceleratorOptions(
                device=AcceleratorDevice.CPU, num_threads=2
            ),
        )
        option_type = PdfFormatOption if fmt == "pdf" else ImageFormatOption
        format_options[input_format] = option_type(pipeline_options=options)
    source = Path(f"/attachment/input.{fmt}")
    # Keep converter chatter out of the machine-readable result. Both streams
    # still count toward the parent supervisor's combined output budget.
    with contextlib.redirect_stdout(sys.stderr):
        result = DocumentConverter(
            allowed_formats=[input_format], format_options=format_options
        ).convert(
            source,
            max_num_pages=200,
            max_file_size=MAX_INPUT_BYTES,
        )
    if result.status != ConversionStatus.SUCCESS or result.errors:
        raise ValueError("conversion incomplete; no evidence accepted")
    if fmt in MODEL_FORMATS:
        verify(Path("/models"), request["model_digest"])
    print(
        json.dumps(
            {
                "document": result.document.export_to_dict(),
                "parser_version": installed,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "model_digest": request.get("model_digest"),
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def convert_office(
    content: bytes,
    *,
    format: str,
    **kwargs,
) -> dict:
    """Office-only compatibility entry point; never silently enables models."""
    if not isinstance(format, str) or format not in OFFICE_FORMATS:
        raise ValueError("supported Office formats: docx, pptx, xlsx")
    return convert_document(content, format=format, **kwargs)


def convert_document(
    content: bytes,
    *,
    format: str,
    source_id: str,
    source_version: str,
    site_packages: Path,
    expected_version: str,
    timeout: float = 120,
    models: Path | None = None,
    model_digest: str | None = None,
) -> dict:
    """Convert an immutable byte snapshot, never an arbitrary URL or live file.

    Dependency directory and expected version are trusted deployment choices.
    Missing dependencies/isolation, partial results and timeouts fail closed.
    No retries, database writes, model downloads or default dependency changes.
    """
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_INPUT_BYTES:
        raise ValueError("attachment must be nonempty bytes within 32 MiB")
    if not isinstance(format, str) or format not in OFFICE_FORMATS | MODEL_FORMATS:
        raise ValueError("unsupported attachment format")
    if format in MODEL_FORMATS:
        if (
            models is None
            or not isinstance(model_digest, str)
            or not re.fullmatch("[0-9a-f]{64}", model_digest)
        ):
            raise ValueError(
                "PDF/image conversion requires a provisioned model bundle and digest"
            )
        if models.is_symlink() or not models.is_dir():
            raise ValueError("model bundle must be a real directory")
        models = models.resolve(strict=True)
        if models == Path("/") or models == Path.home():
            raise ValueError("refusing broad model mount")
    elif models is not None or model_digest is not None:
        raise ValueError("Office conversion does not use model bundles")
    if (
        not isinstance(expected_version, str)
        or not expected_version.strip()
        or len(expected_version) > 100
    ):
        raise ValueError("explicit Docling version required")
    for value in (source_id, source_version):
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ValueError("explicit bounded source metadata required")
    source_hash = hashlib.sha256(content).hexdigest()
    address_limit = 8 * 1024**3 if format in MODEL_FORMATS else 4 * 1024**3
    bootstrap = (
        "import resource\n"
        f"resource.setrlimit(resource.RLIMIT_AS, ({address_limit}, {address_limit}))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "from k3_support.docling_convert import _worker\n_worker()\n"
    )
    argv = sandbox_command(
        package=Path(__file__).parent, site_packages=site_packages, program=bootstrap
    )
    # Private snapshot is the only additional host data exposed to the parser.
    with tempfile.TemporaryDirectory(prefix="codex-docling-") as directory:
        snapshot = Path(directory) / f"input.{format}"
        snapshot.write_bytes(content)
        snapshot.chmod(0o400)
        separator = argv.index("--")
        if models is not None:
            argv[separator:separator] = [
                "--ro-bind",
                str(models),
                "/models",
                "--setenv",
                "HF_HUB_OFFLINE",
                "1",
                "--setenv",
                "OMP_NUM_THREADS",
                "2",
                "--setenv",
                "OPENBLAS_NUM_THREADS",
                "2",
            ]
            separator = argv.index("--")
        argv[separator:separator] = [
            "--dir",
            "/attachment",
            "--ro-bind",
            str(snapshot),
            f"/attachment/input.{format}",
        ]
        output = run_process(
            argv=argv,
            cwd="/",
            env={},
            stdin=json.dumps(
                {
                    "format": format,
                    "expected_version": expected_version,
                    "model_digest": model_digest,
                }
            ).encode(),
            heartbeat=lambda: None,
            timeout=timeout,
            heartbeat_interval=min(1, timeout),
            output_limit=2 * 1024 * 1024,
        )
    result = json.loads(output)
    if (
        not isinstance(result, dict)
        or result.get("parser_version") != expected_version
        or result.get("source_sha256") != source_hash
        or result.get("model_digest") != model_digest
    ):
        raise ValueError("conversion receipt mismatch")
    evidence = import_document(
        json.dumps(result["document"], allow_nan=False).encode(),
        source_id=source_id,
        source_version=source_version,
        source_sha256=source_hash,
        parser_version=expected_version,
        model_revision=model_digest or "not-used",
    )
    evidence["conversion"] = {
        "source_hash_verified": True,
        "network": "disabled",
        "format": format,
        "parser_version_checked": True,
    }
    return evidence
