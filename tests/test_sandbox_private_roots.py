import pytest

from k3_support.remote_sandbox import render_remote_command, sandbox_argv


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_write_permission_requires_real_boolean(value):
    with pytest.raises(ValueError, match="explicit boolean"):
        sandbox_argv(case_id="CASE-1", source_root="/srv/source", worktree_root="/srv/worktrees",
                     repo_paths=[], toolchain_roots=[], writable=value, command="true")
    with pytest.raises(ValueError, match="explicit boolean"):
        render_remote_command(["/usr/bin/bwrap", "--", "true"], writable=value)


@pytest.mark.parametrize("value", ["/srv/source/repo", None, {}, ["/srv/source/repo"] * 501])
def test_repository_grants_require_bounded_list(value):
    with pytest.raises(ValueError, match="bounded list"):
        sandbox_argv(case_id="CASE-1", source_root="/srv/source", worktree_root="/srv/worktrees",
                     repo_paths=value, toolchain_roots=[], writable=False, command="true")


@pytest.mark.parametrize("component", [".aws", ".azure", ".kube", ".docker", ".password-store", ".pki"])
@pytest.mark.parametrize("target", ["source_root", "worktree_root"])
def test_private_credential_directory_cannot_be_project_grant(component, target):
    args = {"case_id": "CASE-1", "source_root": "/srv/source", "worktree_root": "/srv/worktrees",
            "repo_paths": [], "toolchain_roots": [], "writable": False, "command": "true"}
    args[target] = f"/home/operator/{component}/project"
    with pytest.raises(ValueError, match="private"):
        sandbox_argv(**args)
