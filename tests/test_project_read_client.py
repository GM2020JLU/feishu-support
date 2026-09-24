import hashlib
import json
import os
import subprocess

import pytest

from k3_support import project_read_cli
from k3_support.bounded_cli import run
from k3_support.project_read_client import (
    MeegleReadClient,
    ProjectReadError,
    canonical_host,
)

AUTH = {"authenticated": True, "host": "project.feishu.cn", "expires_in_minutes": 0}


@pytest.fixture
def binary(tmp_path):
    path = tmp_path / "fake-meegle"
    path.write_text("#!/usr/bin/python3\nprint('{}')\n")
    path.chmod(0o700)
    return path


def client(binary, responses=None, runner=None):
    calls = []
    pending = list(responses or [])

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        code, value = pending.pop(0)
        return subprocess.CompletedProcess(
            argv, code, json.dumps(value), "private error detail"
        )

    result = MeegleReadClient(
        executable=binary,
        sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        profile="assistant-k3",
        host="project.feishu.cn",
        runner=runner or fake,
    )
    return result, calls


@pytest.mark.parametrize(
    "code,payload,state",
    [
        (0, AUTH, "authenticated"),
        (
            1,
            {"authenticated": False, "host": None, "reason": "no local token"},
            "login_required",
        ),
        (
            1,
            {
                "authenticated": False,
                "host": "project.feishu.cn",
                "reason": "token rejected by server",
            },
            "rejected",
        ),
        (
            2,
            {
                "authenticated": False,
                "host": "project.feishu.cn",
                "reason": "server unreachable: PRIVATE",
            },
            "unavailable",
        ),
    ],
)
def test_auth_distinguishes_recovery_without_exposing_reason(
    binary, code, payload, state
):
    c, calls = client(binary, [(code, payload)])
    status = c.auth_status()
    assert status.state == state
    assert "PRIVATE" not in repr(status)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"authenticated": "true", "host": "project.feishu.cn"}, 0),
        ({"authenticated": True, "host": None}, 0),
        (AUTH, 1),
        ({**AUTH, "expires_in_minutes": True}, 0),
        ({"authenticated": False, "host": None, "reason": {}}, 1),
        ({"authenticated": False, "host": None, "reason": "server unreachable"}, 1),
    ],
)
def test_ambiguous_auth_is_not_authority(binary, payload, code):
    c, calls = client(binary, [(code, payload)])
    with pytest.raises(ProjectReadError, match="invalid_cli_response"):
        c.read_page("user.me", {})
    assert len(calls) == 1


@pytest.mark.parametrize("host", ["meegle.com", "project.feishu.cn:443"])
def test_cross_site_identity_blocks_business_calls(binary, host):
    c, calls = client(binary, [(0, {**AUTH, "host": host})])
    with pytest.raises(ProjectReadError, match="profile_host_mismatch"):
        c.read_page(
            "workitem.get",
            {"project_key": "key", "work_item_id": "1", "fields": ["_all"]},
        )
    assert len(calls) == 1


def test_each_page_rechecks_auth_keeps_profile_numbers_and_opaque_payload(
    binary, monkeypatch
):
    for key in ("MEEGLE_USER_ACCESS_TOKEN", "LD_PRELOAD", "NODE_OPTIONS", "BASH_ENV"):
        monkeypatch.setenv(key, "DO_NOT_PROPAGATE")
    raw = {"unverified_shape": [1], "next_page_token": "last-field"}
    c, calls = client(
        binary,
        [
            (0, AUTH),
            (0, raw),
            (
                1,
                {
                    "authenticated": False,
                    "host": "project.feishu.cn",
                    "reason": "token rejected by server",
                },
            ),
        ],
    )
    params = {
        "project_key": "key;ignored",
        "work_item_id": "1",
        "fields": ["_all"],
        "page_size": 100,
    }
    page = c.read_page("workitem.get", params)
    assert page["completeness"] == "page_only" and page["payload"] == raw
    argv, options = calls[1]
    assert argv[:3] == [str(binary), "--profile", "assistant-k3"]
    assert json.loads(argv[argv.index("--params") + 1]) == params
    assert not any(value == "DO_NOT_PROPAGATE" for value in options["env"].values())
    assert options["env"].get("HOME") == os.environ.get("HOME")
    # Keyring-backed CLI tokens need the session bus; these are bus locators,
    # not credentials (observed login_required regression, 2026-09-18). USER
    # feeds the CLI file-credential machine key (second regression, same day).
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "USER", "LOGNAME"):
        assert options["env"].get(key) == os.environ.get(key)
    assert options["stdout_limit"] == 4 * 1024 * 1024
    with pytest.raises(ProjectReadError, match="auth_rejected"):
        c.read_page("workitem.get", params | {"page_token": "last-field"})
    assert len(calls) == 3


@pytest.mark.parametrize(
    "command,params",
    [
        ("workitem.update", {}),
        ("comment.add", {}),
        ("auth.login", {}),
        ("config.show", {}),
        ("workitem.query", {"mql": "SELECT *"}),
        ("user.me", {"host": "other"}),
        ("project.search", {"project_key": "key", "page_num": True}),
        (
            "workitem.get",
            {
                "project_key": "key",
                "work_item_id": "1",
                "fields": ["name"],
                "page_token": "x",
            },
        ),
        (
            "workitem.get",
            {"project_key": "key", "work_item_id": "1", "fields": ["_all", "name"]},
        ),
        (
            "workitem.get",
            {
                "project_key": "key",
                "work_item_id": "1",
                "fields": ["_all"],
                "page_size": "100",
            },
        ),
        (
            "workitem.get",
            {
                "project_key": "key",
                "work_item_id": "1",
                "fields": ["_all"],
                "page_size": 201,
            },
        ),
    ],
)
def test_writes_and_invalid_parameters_never_start_client(binary, command, params):
    c, calls = client(binary)
    with pytest.raises(ProjectReadError, match="invalid_read_request"):
        c.read_page(command, params)
    assert calls == []


def test_official_decode_does_not_resolve_space_or_forward_raw_diagnostics(binary):
    c, calls = client(
        binary,
        [
            (
                0,
                {
                    "url_kind": "workitem_detail",
                    "host": "PROJECT.FEISHU.CN.",
                    "simple_name": "example-space",
                    "work_item_type": "issue",
                    "work_item_id": "1234567890",
                    "raw": "PRIVATE",
                    "query": {"private": "PRIVATE"},
                },
            )
        ],
    )
    result = c.decode_workitem_url(
        "https://project.feishu.cn/example-space/issue/detail/1234567890"
    )
    assert result["project_key_resolved"] is False
    assert "PRIVATE" not in json.dumps(result)
    assert calls[0][0][3:5] == ["url", "decode"]
    assert len(calls) == 1  # Local decoder does not trigger login.


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/k/issue/detail/1",
        "https://u:p@project.feishu.cn/k/issue/detail/1",
        "http://project.feishu.cn/k/issue/detail/1",
        "https://project.feishu.cn/k/issue/detail/1?token=secret",
        "https://project.feishu.cn/k/issue/detail/1#token",
        "/k/issue/detail/1",
    ],
)
def test_unsafe_url_rejected_before_cli(binary, url):
    c, calls = client(binary)
    with pytest.raises(ProjectReadError, match="invalid_workitem_url"):
        c.decode_workitem_url(url)
    assert not calls


@pytest.mark.parametrize(
    "extra", [{"url_kind": "unknown"}, {"is_resource": True}, {"work_item_id": None}]
)
def test_non_bug_detail_decode_never_guessed(binary, extra):
    c, _ = client(
        binary,
        [
            (
                0,
                {
                    "url_kind": "workitem_detail",
                    "host": "project.feishu.cn",
                    "simple_name": "k",
                    "work_item_type": "issue",
                    "work_item_id": "1",
                    **extra,
                },
            )
        ],
    )
    with pytest.raises(ProjectReadError, match="unsupported_workitem_url"):
        c.decode_workitem_url("https://project.feishu.cn/k/issue/detail/1")


def test_inspect_refresh_is_read_only_and_authenticated(binary):
    calls = []
    help_text = "\n  meegle workitem get\n  Read work item\n\n  --fields <array>\n"

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(AUTH) if len(calls) == 1 else help_text, ""
        )

    c, _ = client(binary, runner=runner)
    assert c.inspect("workitem.get") == {
        "command": "workitem.get",
        "format": "text",
        "text": help_text.strip(),
        "machine_schema": False,
    }
    assert calls[1][0][3:6] == ["--refresh", "inspect", "workitem.get"]
    with pytest.raises(ProjectReadError):
        c.inspect("workitem.create")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "code,text,error",
    [
        (1, "PRIVATE", "remote_read_failed"),
        (0, "", "invalid_cli_response"),
        (0, "meegle workitem create\nPRIVATE", "invalid_cli_response"),
        (0, '{"error":"PRIVATE"}', "invalid_cli_response"),
        (0, "meegle workitem get\n\x1bPRIVATE", "invalid_cli_response"),
    ],
)
def test_inspect_rejects_wrong_contract_without_echoing_payload(
    binary, code, text, error
):
    def runner(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(AUTH), "")
        return subprocess.CompletedProcess(argv, code, text, "PRIVATE")

    c, _ = client(binary, runner=runner)
    with pytest.raises(ProjectReadError, match=error) as exc:
        c.inspect("workitem.get")
    assert "PRIVATE" not in str(exc.value)


def test_changed_binary_and_writable_executable_never_run(binary):
    c, calls = client(binary)
    binary.write_text("#!/usr/bin/python3\nprint('changed')\n")
    with pytest.raises(ProjectReadError, match="client_binary_changed"):
        c.auth_status()
    c, calls = client(binary)
    binary.chmod(0o777)
    with pytest.raises(ProjectReadError, match="untrusted_client_binary"):
        c.auth_status()
    assert not calls


@pytest.mark.parametrize(
    "text",
    [
        '{"authenticated":true,"authenticated":false}',
        '{"bad":NaN}',
        "PRIVATE not JSON",
        "null",
        '"PRIVATE"',
        '{"ok":1}\n{"ok":2}',
    ],
)
def test_corrupt_output_is_never_accepted_or_echoed(binary, text):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, text, "PRIVATE")

    c, _ = client(binary, runner=runner)
    with pytest.raises(ProjectReadError) as exc:
        c.auth_status()
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("import time; time.sleep(5)", "read_timeout"),
        ("print('x' * 5000000)", "response_too_large"),
        ("import os; os.write(1, b'\\xff')", "client_execution_failed"),
    ],
)
def test_real_process_limits_are_mapped_without_leaking_output(binary, body, expected):
    binary.write_text("#!/usr/bin/python3\n" + body + "\n")

    def short_run(argv, **kwargs):
        return run(argv, **(kwargs | {"timeout": 0.5}))

    c, _ = client(binary, runner=short_run)
    with pytest.raises(ProjectReadError, match=expected):
        c.auth_status()


def test_local_cli_works_with_real_subprocess_and_no_database(binary, capsys):
    binary.write_text(
        "#!/usr/bin/python3\nimport json\nprint(json.dumps(" + repr(AUTH) + "))\n"
    )
    args = [
        "--executable",
        str(binary),
        "--sha256",
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        "--profile",
        "assistant-k3",
        "--host",
        "project.feishu.cn",
        "auth-status",
    ]
    assert project_read_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "authenticated"
    binary.write_text("#!/usr/bin/python3\nprint('PRIVATE')\n")
    assert project_read_cli.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "client_binary_changed"}


def test_host_normalization_preserves_explicit_port():
    assert canonical_host("Project.Feishu.Cn.") == "project.feishu.cn"
    assert canonical_host("project.feishu.cn:443") != canonical_host(
        "project.feishu.cn"
    )
    for invalid in (
        "https://project.feishu.cn",
        "x@project.feishu.cn",
        "../x",
        "x:0",
        "x:65536",
    ):
        with pytest.raises(ProjectReadError):
            canonical_host(invalid)
