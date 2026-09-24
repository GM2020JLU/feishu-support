# ruff: noqa: F811 -- shared isolated fixtures
import copy
import hashlib
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_project_activity import context  # noqa: F401
from test_project_attachments import FILE, pages
from test_project_read_client import binary  # noqa: F401
from test_project_read_snapshot import DEST, Client, bound  # noqa: F401

from k3_support import project_activity as activity
from k3_support import project_attachment_download as download
from k3_support import project_attachment_transfer as transfer
from k3_support import project_bug_grants as grants
from k3_support.project_read_client import ProjectReadError

PLAN = {
    "download_url": "https://project.feishu.cn/file/:part_number?private=signature",
    "sign": "signature",
    "is_multipart": True,
    "target_unit_code": "unit",
    "multipart": {
        "part_count": 2,
        "part_size": 3,
        "need": [
            {"part_index": 0, "start_byte": 0, "end_byte": 2},
            {"part_index": 1, "start_byte": 3, "end_byte": 4},
        ],
    },
}
DATA = b"abcde"


class Response(io.BytesIO):
    status = 200

    def __init__(self, content, headers=None):
        super().__init__(content)
        self.headers = {
            transfer.SIGN_HEADER: "signature",
            "Content-Length": str(len(content)),
        }
        self.headers.update(headers or {})

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


def transport(responses):
    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "project.feishu.cn"

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            pass

    return Connection, calls


def test_multipart_verifies_ranges_headers_bytes_and_local_hash():
    factory, calls = transport([Response(b"abc"), Response(b"de")])
    sink = io.BytesIO()
    result = transfer.transfer(PLAN, sink, connection_factory=factory)
    assert (
        sink.getvalue() == DATA and result["sha256"] == hashlib.sha256(DATA).hexdigest()
    )
    assert result["parts"] == 2 and result["size_bytes"] == 5
    assert all(c[2]["x-target-unit"] == "unit" for c in calls)
    assert calls[0][1].startswith("/file/0?") and calls[1][1].startswith("/file/1?")
    assert all("Authorization" not in c[2] for c in calls)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/f",
        "https://u:p@example.com/f",
        "https://example.com:8443/f",
        "https://example.com/f#frag",
        "https://example.com/f\n",
    ],
)
def test_invalid_urls_never_connect(url):
    with pytest.raises(ValueError):
        transfer.validate(PLAN | {"download_url": url})


@pytest.mark.parametrize(
    "change",
    [{"part_index": 7}, {"start_byte": 1}, {"end_byte": 8}, {"end_byte": True}],
)
def test_invalid_ranges_never_connect(change):
    plan = copy.deepcopy(PLAN)
    plan["multipart"]["need"][0].update(change)
    with pytest.raises(ValueError):
        transfer.validate(plan)


@pytest.mark.parametrize(
    "headers",
    [
        {transfer.SIGN_HEADER: "wrong"},
        {transfer.SIGN_HEADER: ""},
        {"Content-Encoding": "gzip"},
        {"Content-Length": "9"},
    ],
)
def test_unsigned_encoded_or_short_parts_fail(headers):
    factory, _ = transport([Response(b"abc", headers)])
    with pytest.raises(ValueError):
        transfer.transfer(PLAN, io.BytesIO(), connection_factory=factory)


def test_redirect_never_followed():
    response = Response(b"")
    response.status = 302
    factory, calls = transport([response])
    with pytest.raises(ValueError):
        transfer.transfer(PLAN, io.BytesIO(), connection_factory=factory)
    assert len(calls) == 1


def test_budget_enforced_without_content_length(monkeypatch):
    response = Response(b"abcde")
    response.headers.pop("Content-Length")
    factory, _ = transport([response])
    monkeypatch.setattr(transfer, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="budget"):
        transfer.transfer(
            PLAN | {"is_multipart": False}, io.BytesIO(), connection_factory=factory
        )


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_private_dns_never_opens_socket(monkeypatch, address):
    monkeypatch.setattr(
        transfer.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))]
    )
    with pytest.raises(ValueError, match="non-public"):
        transfer.PinnedHTTPS("project.feishu.cn").connect()


def accepted(conn, config, context):
    queued = activity.enqueue(conn, config, **(context | {"kind": "attachments"}))
    result = activity.run_one(
        conn, lambda: config, client_factory=lambda _: Client(pages([FILE]))
    )
    assert result["state"] == "succeeded"
    return {
        "activity_id": queued["activity_id"],
        "field_key": "progress",
        "source_digest": download.digest(FILE),
    }


def runner(argv, **kwargs):
    assert "-I" in argv and kwargs["timeout"] == 60
    assert set(kwargs["env"]) == {"PATH", "LANG"}
    path = Path(argv[-1])
    path.write_bytes(DATA)
    path.chmod(0o600)
    return subprocess.CompletedProcess(
        argv,
        0,
        json.dumps(
            {
                "size_bytes": 5,
                "sha256": hashlib.sha256(DATA).hexdigest(),
                "parts": 2,
                "server_signature_checked": True,
            }
        ),
        "",
    )


def native_client(values=None):
    client = Client(values if values is not None else pages([FILE]) + pages([FILE]))
    client.prepare_attachment_download = lambda destination, url: copy.deepcopy(PLAN)
    return client


def test_source_bound_queue_download_and_reverified_local_delivery(
    conn, config, context, monkeypatch
):
    source = accepted(conn, config, context)
    original = download.collect
    monkeypatch.setattr(
        download, "collect", lambda *a, **kw: original(*a, **kw, runner=runner)
    )
    args = source | {
        "actor": context["actor"],
        "grant_id": context["grant_id"],
        "request_id": "download",
    }
    queued = download.enqueue(conn, config, **args)
    result = activity.run_one(
        conn, lambda: config, client_factory=lambda _: native_client()
    )
    assert result["state"] == "succeeded", result
    assert (
        download.enqueue(conn, config, **args)["activity_id"] == queued["activity_id"]
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_activity_requests SET source_json='{}' WHERE activity_id=?",
            (queued["activity_id"],),
        )
    stream, receipt = download.open_artifact(
        conn, config, actor=context["actor"], activity_id=queued["activity_id"]
    )
    with stream:
        assert stream.read() == DATA
    assert receipt["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert (
        conn.execute(
            "SELECT count(*) FROM project_bug_events WHERE kind='attachment_delivery_authorized'"
        ).fetchone()[0]
        == 1
    )
    root = download.storage(config)
    assert not list(root.glob("*/plan.json"))
    (root / receipt["artifact_id"] / "content").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="verification"):
        download.open_artifact(
            conn, config, actor=context["actor"], activity_id=queued["activity_id"]
        )


def test_changed_source_rejects_before_transfer(config):
    calls = []
    with pytest.raises(ProjectReadError, match="attachment_source_changed"):
        download.collect(
            native_client(pages([FILE | {"url": "other"}])),
            config,
            DEST,
            {"field_key": "progress", "source_digest": download.digest(FILE)},
            guard=lambda: None,
            runner=lambda *a, **kw: calls.append(a),
        )
    assert calls == []


def test_changed_source_after_transfer_removes_unaccepted_bytes(config):
    with pytest.raises(ProjectReadError, match="attachment_source_changed"):
        download.collect(
            native_client(pages([FILE]) + pages([])),
            config,
            DEST,
            {"field_key": "progress", "source_digest": download.digest(FILE)},
            guard=lambda: None,
            runner=runner,
        )
    assert list(download.storage(config).glob("file-*")) == []


def test_timeout_cleans_partial_and_private_plan(config):
    def timeout(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(ProjectReadError, match="attachment_transfer_timeout"):
        download.collect(
            native_client(),
            config,
            DEST,
            {"field_key": "progress", "source_digest": download.digest(FILE)},
            guard=lambda: None,
            runner=timeout,
        )
    assert list(download.storage(config).glob("file-*")) == []


def test_cross_actor_or_fabricated_source_rejected(conn, config, context):
    source = accepted(conn, config, context)
    with pytest.raises(ValueError):
        download.selection(conn, "other", source)
    with pytest.raises(ValueError):
        download.selection(
            conn, context["actor"], source | {"source_digest": "invented"}
        )


def test_expired_grant_never_starts_download(conn, config, context):
    source = accepted(conn, config, context)
    grants.revoke(conn, actor=context["actor"], grant_id=context["grant_id"])
    with pytest.raises(PermissionError):
        download.enqueue(
            conn,
            config,
            actor=context["actor"],
            **source,
            grant_id=context["grant_id"],
            request_id="denied",
        )


@pytest.mark.parametrize("header", ["bytes 0-2/5", "bytes 0-2/6", None, "bytes 1-3/5"])
def test_official_206_requires_exact_plan_range(header):
    first, second = Response(b"abc"), Response(b"de")
    first.status = second.status = 206
    if header is not None:
        first.headers["Content-Range"] = header
    second.headers["Content-Range"] = "bytes 3-4/5"
    factory, _ = transport([first, second])
    if header == "bytes 0-2/5":
        assert (
            transfer.transfer(PLAN, io.BytesIO(), connection_factory=factory)[
                "size_bytes"
            ]
            == 5
        )
    else:
        with pytest.raises(ValueError):
            transfer.transfer(PLAN, io.BytesIO(), connection_factory=factory)


def test_single_206_must_cover_entire_object():
    response = Response(b"abc")
    response.status = 206
    response.headers["Content-Range"] = "bytes 0-2/5"
    factory, _ = transport([response])
    with pytest.raises(ValueError, match="incomplete response range"):
        transfer.transfer(
            PLAN | {"is_multipart": False}, io.BytesIO(), connection_factory=factory
        )


def test_prepare_download_uses_only_control_profile_and_bound_source(binary):
    from test_project_read_client import AUTH, client

    reader, calls = client(binary, [(0, AUTH), (0, PLAN)])
    assert reader.prepare_attachment_download(DEST, FILE["url"]) == PLAN
    args = calls[-1][0]
    assert args[args.index("attachment") + 1] == "prepare-download"
    params = json.loads(args[args.index("--params") + 1])
    assert params == {
        "project_key": "space",
        "work_item_id": "123",
        "file_url": FILE["url"],
    }
    with pytest.raises(ProjectReadError):
        reader.prepare_attachment_download(
            DEST | {"host": "other.example"}, FILE["url"]
        )
    assert len(calls) == 2


def test_retained_content_requires_live_grant_and_policy(
    conn, config, context, monkeypatch
):
    source = accepted(conn, config, context)
    original = download.collect
    monkeypatch.setattr(
        download, "collect", lambda *a, **kw: original(*a, **kw, runner=runner)
    )
    queued = download.enqueue(
        conn,
        config,
        actor=context["actor"],
        **source,
        grant_id=context["grant_id"],
        request_id="retained",
    )
    assert (
        activity.run_one(
            conn, lambda: config, client_factory=lambda _: native_client()
        )["state"]
        == "succeeded"
    )
    config.raw["mode"] = "drain"
    with pytest.raises(PermissionError, match="paused"):
        download.open_artifact(
            conn, config, actor=context["actor"], activity_id=queued["activity_id"]
        )
    config.raw["mode"] = "shadow"
    grants.revoke(conn, actor=context["actor"], grant_id=context["grant_id"])
    with pytest.raises(PermissionError):
        download.open_artifact(
            conn, config, actor=context["actor"], activity_id=queued["activity_id"]
        )
