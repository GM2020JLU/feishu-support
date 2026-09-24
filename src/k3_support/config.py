from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .timeutil import utc_now


class ConfigError(ValueError):
    pass


ALLOWED_TOP_LEVEL = {
    "schema_version",
    "mode",
    "timezone",
    "work_hours",
    "paths",
    "features",
    "notifications",
    "coordination",
    "ingress",
    "routing",
    "mail",
    "knowledge_retrieval",
    "knowledge_release",
    "policy",
    "identity",
    "scope",
    "repositories",
    "base",
    "runtime",
    "coding_executors",
    "operator_notifications",
    "project_integration",
}
REQUIRED_TOP_LEVEL = ALLOWED_TOP_LEVEL - {
    "project_integration",
    "coding_executors",
    "operator_notifications",
    "runtime",
    "notifications",
    "routing",
    "coordination",
    "ingress",
    "mail",
    "knowledge_retrieval",
    "knowledge_release",
}

FEATURES = {
    "shadow_reply",
    "auto_faq",
    "codex",
    "board",
    "wip_push",
    "mail",
    "calendar",
    "base_sync",
}
NOTIFICATION_DEFAULTS = {
    "telegram_p0": True,
    "feishu_p0_message": True,
    "feishu_app_urgent": False,
    "feishu_sms_urgent": False,
}
ROUTING_DEFAULTS = {
    "ai_enabled": True,
    "minimum_route_confidence": 0.8,
    "minimum_clarify_confidence": 0.92,
    "max_clarifications_per_case": 1,
    "profile_ttl_hours": 168,
    "org_profile_lookup": False,
}
COORDINATION_DEFAULTS = {
    "work_hours_send_grace_seconds": 60,
    "off_hours_send_grace_seconds": 15,
    "claim_reaction": "OnIt",
}
INGRESS_DEFAULTS = {
    "poll_lookback_seconds": 300,
}
MAIL_DEFAULTS = {
    "summary_share_chat_id": None,
    "max_important_per_summary": 8,
    "max_body_chars_per_message": 6000,
    "max_input_chars": 180000,
}
KNOWLEDGE_RETRIEVAL_DEFAULTS = {
    "backend": "sqlite",
    "candidate_limit": 20,
    "prefetch_limit": 80,
    "allow_fallback": True,
    "qdrant_path": None,
    "qdrant_collection": "knowledge",
}
SAFE_POSIX_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def _runtime_defaults(*, schema_version: int = 2) -> dict[str, Any]:
    # Keep the virtualenv path. Resolving the interpreter symlink would point at
    # the system Python directory and make adjacent console scripts disappear.
    executable_dir = Path(sys.executable).absolute().parent
    defaults: dict[str, Any] = {
        "remote_transport": "ssh",
        "remote_host": None,
        "remote_workspace_root": None,
        "remote_source_root": None,
        "remote_worktree_root": None,
        "remote_toolchain_roots": [],
        "remote_receipt_directory": None,
        "ssh_command": "ssh",
        "serial_command": "serial",
        "board_serial_socket": None,
        "board_serial_daemon_uid": None,
        "lark_cli_command": "lark-cli",
        "hermes_command": "hermes",
        "semantic_command": str(executable_dir / "k3-support-hermes-stdin"),
        "board_control_script": None,
        "board_boot_script": None,
        "codex_remote_command": str(executable_dir / "k3-codex-remote"),
        "codex_board_command": str(executable_dir / "k3-codex-board"),
    }
    if schema_version == 1:
        # Preserve the original installation's effective targets until an
        # operator explicitly migrates its config. New v2 instances never use
        # these defaults; see config_migration.preview_config_migration.
        defaults.update({
            "remote_host": "buildhost",
            "remote_workspace_root": "/data/home2/operator/WorkSpace",
            "remote_source_root": "/data/home2/operator/WorkSpace/k3",
            "remote_worktree_root": "/data/home2/operator/WorkSpace/k3-ai-worktrees",
            "board_control_script": str(Path.home() / ".codex/skills/board-ctrl/scripts/board-ctrl.sh"),
            "board_boot_script": str(Path.home() / ".codex/skills/board-ctrl/scripts/fastboot-boot.sh"),
        })
    return defaults


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: Path
    source_config_guard: Any = field(default=None, repr=False, compare=False)

    @property
    def database_path(self) -> Path:
        return Path(self.raw["paths"]["database"]).expanduser()

    @property
    def data_dir(self) -> Path:
        return Path(self.raw["paths"]["data_dir"]).expanduser()

    @property
    def mode(self) -> str:
        return self.raw["mode"]

    def feature(self, name: str) -> bool:
        if name not in FEATURES:
            raise ConfigError(f"unknown feature: {name}")
        from .feature_settings import effective

        return effective(self)[name]

    @property
    def work_hours(self) -> dict[str, str]:
        from .work_hours_settings import effective

        return effective(self)

    def notification(self, name: str) -> bool:
        if name not in NOTIFICATION_DEFAULTS:
            raise ConfigError(f"unknown notification channel: {name}")
        return bool(self.raw["notifications"][name])

    def runtime(self, name: str) -> str:
        value = self.raw["runtime"].get(name)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"runtime value is unavailable: {name}")
        return value

    def ingress(self, name: str) -> int:
        if name not in INGRESS_DEFAULTS:
            raise ConfigError(f"unknown ingress setting: {name}")
        return int(self.raw["ingress"][name])

    def mail(self, name: str) -> Any:
        if name not in MAIL_DEFAULTS:
            raise ConfigError(f"unknown mail setting: {name}")
        value = self.raw["mail"][name]
        if name == "summary_share_chat_id" and value is None:
            # Reuse the already reviewed private owner-alert destination. This
            # avoids duplicating a stable chat ID in every deployment profile.
            return self.raw["identity"].get("feishu_p0_chat_id")
        return value

    @property
    def control_operator_id(self) -> str | None:
        """Deployment identity for authenticated web operations; legacy fallback."""
        value = self.raw["identity"].get("control_operator_id")
        return value if value is not None else self.telegram_control_user_id

    @property
    def web_control_chat_id(self) -> str | None:
        return "web" if self.raw["identity"].get("control_operator_id") else self.telegram_control_chat_id

    @property
    def telegram_control_user_id(self) -> str | None:
        value = self.raw["identity"].get("telegram_control_user_id")
        return str(value) if value is not None else None

    @property
    def telegram_control_chat_id(self) -> str | None:
        value = self.raw["identity"].get("telegram_control_chat_id")
        return str(value) if value is not None else None


def is_work_time(config: Config, at: datetime | None = None) -> bool:
    """Interpret the instance's daily half-open interval in its named timezone."""
    observed = at or utc_now()
    if observed.tzinfo is None:
        raise ConfigError("work-time observation must include a timezone")
    from .notification_schedule import window

    return not window(config, observed)["active"]


def _clock_minutes(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ConfigError("work_hours values must be HH:MM in 00:00-23:59")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    data = copy.deepcopy(data)
    unknown = set(data) - ALLOWED_TOP_LEVEL
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    missing = REQUIRED_TOP_LEVEL - set(data)
    if missing:
        raise ConfigError(f"missing top-level keys: {', '.join(sorted(missing))}")
    if type(data["schema_version"]) is not int or data["schema_version"] not in {1, 2}:
        raise ConfigError("schema_version must be 1 or 2")
    if data["mode"] not in {"shadow", "active", "drain"}:
        raise ConfigError("mode must be shadow, active, or drain")
    # Optional opt-in; absence preserves existing configuration fingerprints.
    project = data.get("project_integration")
    if "project_integration" in data:
        if not isinstance(project, dict) or "write_enabled" not in project or set(project) - {"write_enabled", "reader", "intake_spaces", "search_spaces", "comment_writer", "field_writer"} or type(project["write_enabled"]) is not bool:
            raise ConfigError("project_integration requires a boolean write_enabled")
        if "comment_writer" in project:
            from .project_comment_writer_config import validate as validate_comment_writer
            try:
                validate_comment_writer(project["comment_writer"])
            except ValueError as exc:
                raise ConfigError("invalid Project comment writer") from exc
        if "field_writer" in project:
            from .project_field_writer_config import validate as validate_field_writer
            try:
                validate_field_writer(project["field_writer"])
            except ValueError as exc:
                raise ConfigError("invalid Project field writer") from exc
        if "reader" in project:
            from .project_reader_config import validate as validate_project_reader
            try:
                validate_project_reader(project["reader"])
            except ValueError as exc:
                raise ConfigError("invalid Project reader configuration") from exc
        if "intake_spaces" in project:
            from .project_intake_policy import validate as validate_intake_spaces
            try:
                validate_intake_spaces(project["intake_spaces"])
            except ValueError as exc:
                raise ConfigError("invalid Project intake spaces") from exc
        if "search_spaces" in project:
            from .project_bug_query import validate_spaces
            try:
                validate_spaces(project["search_spaces"])
            except ValueError as exc:
                raise ConfigError("invalid Project search spaces") from exc
        if project["write_enabled"] and data["mode"] != "active":
            raise ConfigError("Project writes require active mode")
    retrieval_value = data.get("knowledge_retrieval", {})
    if not isinstance(retrieval_value, dict) or set(retrieval_value) - set(KNOWLEDGE_RETRIEVAL_DEFAULTS):
        raise ConfigError("knowledge_retrieval keys do not match schema")
    retrieval = {**KNOWLEDGE_RETRIEVAL_DEFAULTS, **retrieval_value}
    if retrieval["backend"] not in {"sqlite", "qdrant"}:
        raise ConfigError("knowledge_retrieval backend must be sqlite or qdrant")
    candidate_limit, prefetch_limit = retrieval["candidate_limit"], retrieval["prefetch_limit"]
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 100:
        raise ConfigError("knowledge_retrieval candidate_limit must be an integer in 1..100")
    if type(prefetch_limit) is not int or not candidate_limit <= prefetch_limit <= 500:
        raise ConfigError("knowledge_retrieval prefetch_limit must be an integer in candidate_limit..500")
    if type(retrieval["allow_fallback"]) is not bool:
        raise ConfigError("knowledge_retrieval allow_fallback must be boolean")
    index_path = retrieval["qdrant_path"]
    if index_path is not None and (
        not isinstance(index_path, str) or not index_path.strip()
        or not Path(index_path).is_absolute() or ".." in Path(index_path).parts
    ):
        raise ConfigError("knowledge_retrieval qdrant_path must be an absolute local path or null")
    collection = retrieval["qdrant_collection"]
    if not isinstance(collection, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", collection):
        raise ConfigError("knowledge_retrieval qdrant_collection is invalid")
    data["knowledge_retrieval"] = retrieval
    release_value = data.get("knowledge_release", {})
    if not isinstance(release_value, dict) or set(release_value) - {"artifact_path", "trust_policy_path"}:
        raise ConfigError("knowledge_release keys do not match schema")
    release = {"artifact_path": None, "trust_policy_path": None, **release_value}
    for key, value in release.items():
        if value is not None and (
            not isinstance(value, str) or not value.strip() or not Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            raise ConfigError(f"knowledge_release {key} must be an absolute local path or null")
    data["knowledge_release"] = release
    try:
        if not isinstance(data["timezone"], str):
            raise TypeError("not a string")
        ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ConfigError("timezone must be a valid IANA timezone") from exc

    work_hours = _require_mapping(data, "work_hours")
    if set(work_hours) != {"start", "end"}:
        raise ConfigError("work_hours must contain start and end")
    if _clock_minutes(work_hours["start"]) == _clock_minutes(work_hours["end"]):
        raise ConfigError("work_hours start and end must differ")

    paths = _require_mapping(data, "paths")
    if set(paths) != {"data_dir", "database"}:
        raise ConfigError("paths must contain only data_dir and database")

    runtime_value = data.get("runtime", {})
    if not isinstance(runtime_value, dict):
        raise ConfigError("runtime must be a mapping")
    defaults = _runtime_defaults(schema_version=data["schema_version"])
    if set(runtime_value) - set(defaults):
        raise ConfigError("runtime keys do not match schema")
    runtime = {**defaults, **runtime_value}
    features = _require_mapping(data, "features")
    remote_required = bool(
        features.get("codex") or features.get("wip_push") or data.get("repositories")
    )
    roots = ("remote_workspace_root", "remote_source_root", "remote_worktree_root")
    roots_configured = any(runtime[key] is not None for key in roots)
    if remote_required or roots_configured:
        for key in (*roots, "remote_host"):
            if runtime[key] is None:
                raise ConfigError(f"configure runtime {key} explicitly for remote capabilities")
    for key in (
        "remote_workspace_root",
        "remote_source_root",
        "remote_worktree_root",
    ):
        value = runtime[key]
        if value is None and not remote_required and not roots_configured:
            continue
        if not isinstance(value, str):
            raise ConfigError(f"runtime {key} must be an absolute safe POSIX path")
        path = PurePosixPath(value)
        if (
            not value.startswith("/")
            or ".." in path.parts
            or any(not SAFE_POSIX_COMPONENT.fullmatch(part) for part in path.parts[1:])
        ):
            raise ConfigError(f"runtime {key} must be an absolute safe POSIX path")
    workspace_root = PurePosixPath(runtime["remote_workspace_root"]) if roots_configured else None
    source_root = PurePosixPath(runtime["remote_source_root"]) if roots_configured else None
    worktree_root = PurePosixPath(runtime["remote_worktree_root"]) if roots_configured else None
    if roots_configured and workspace_root not in source_root.parents:
        raise ConfigError("remote_source_root must be under remote_workspace_root")
    if roots_configured and workspace_root not in worktree_root.parents:
        raise ConfigError("remote_worktree_root must be under remote_workspace_root")
    if roots_configured and workspace_root.parent == PurePosixPath("/"):
        raise ConfigError("remote_workspace_root must have a non-root parent")
    if roots_configured and (source_root in worktree_root.parents or worktree_root in source_root.parents):
        raise ConfigError("remote source and worktree roots must not contain each other")
    if roots_configured:
        from .remote_sandbox import validate_toolchain_roots

        try:
            runtime["remote_toolchain_roots"] = validate_toolchain_roots(
                runtime["remote_toolchain_roots"],
                source_root=runtime["remote_source_root"],
                worktree_root=runtime["remote_worktree_root"],
            )
        except ValueError as exc:
            raise ConfigError(f"runtime remote_toolchain_roots: {exc}") from exc
    elif runtime["remote_toolchain_roots"] != []:
        raise ConfigError("configure remote roots before granting remote_toolchain_roots")
    from .board_serial_observer import endpoint
    from .remote_guard import receipt_directory
    try:
        receipt_directory(runtime)
        endpoint(runtime)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if runtime["remote_transport"] not in ("ssh", "local"):
        raise ConfigError("runtime remote_transport must be ssh or local")
    if runtime["remote_transport"] == "local" and runtime["remote_host"] != "localhost":
        raise ConfigError("local execution requires explicit runtime remote_host: localhost")
    host = runtime["remote_host"]
    if host is not None and (
        not isinstance(host, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,254}", host)
    ):
        raise ConfigError("runtime remote_host is invalid")
    for key in (
        "ssh_command",
        "serial_command",
        "lark_cli_command",
        "hermes_command",
        "semantic_command",
        "codex_remote_command",
        "codex_board_command",
    ):
        value = runtime[key]
        if not isinstance(value, str) or not value or "\x00" in value or any(
            ch.isspace() for ch in value
        ):
            raise ConfigError(f"runtime {key} must be one executable argv item")
        if "/" in value and not Path(value).expanduser().is_absolute():
            raise ConfigError(f"runtime {key} must be a command name or absolute path")
        runtime[key] = str(Path(value).expanduser()) if "/" in value else value
    for key in ("board_control_script", "board_boot_script"):
        value = runtime[key]
        if value is None and not features.get("board"):
            continue
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ConfigError(f"runtime {key} must be an absolute path")
        runtime[key] = str(Path(value).expanduser())
    data["runtime"] = runtime

    features = _require_mapping(data, "features")
    if set(features) != FEATURES or not all(isinstance(v, bool) for v in features.values()):
        raise ConfigError("features must contain every known boolean feature exactly once")
    if data["mode"] == "shadow" and any(
        features[name] for name in ("auto_faq", "codex", "board", "wip_push", "calendar")
    ):
        raise ConfigError("shadow mode cannot enable external or executor features")

    notifications = data.get("notifications", NOTIFICATION_DEFAULTS)
    if not isinstance(notifications, dict):
        raise ConfigError("notifications must be a mapping")
    if set(notifications) != set(NOTIFICATION_DEFAULTS) or not all(
        isinstance(value, bool) for value in notifications.values()
    ):
        raise ConfigError(
            "notifications must contain every known boolean channel exactly once"
        )
    data["notifications"] = copy.deepcopy(notifications)

    routing = data.get("routing", ROUTING_DEFAULTS)
    if not isinstance(routing, dict) or set(routing) != set(ROUTING_DEFAULTS):
        raise ConfigError("routing keys do not match schema")
    if not isinstance(routing["ai_enabled"], bool) or not isinstance(
        routing["org_profile_lookup"], bool
    ):
        raise ConfigError("routing AI and organization switches must be boolean")
    for key in ("minimum_route_confidence", "minimum_clarify_confidence"):
        value = routing[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.8 <= value <= 1:
            raise ConfigError(f"routing {key} must be between 0.8 and 1")
    if routing["max_clarifications_per_case"] != 1:
        raise ConfigError("routing allows exactly one clarification per Case")
    if (
        isinstance(routing["profile_ttl_hours"], bool)
        or not isinstance(routing["profile_ttl_hours"], int)
        or not 1 <= routing["profile_ttl_hours"] <= 720
    ):
        raise ConfigError("routing profile_ttl_hours must be between 1 and 720")
    data["routing"] = copy.deepcopy(routing)

    coordination = data.get("coordination", COORDINATION_DEFAULTS)
    if not isinstance(coordination, dict) or set(coordination) != set(COORDINATION_DEFAULTS):
        raise ConfigError("coordination keys do not match schema")
    for key in ("work_hours_send_grace_seconds", "off_hours_send_grace_seconds"):
        value = coordination[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 300:
            raise ConfigError(f"coordination {key} must be between 0 and 300 seconds")
    if coordination["claim_reaction"] != "OnIt":
        raise ConfigError("coordination claim_reaction must be OnIt")
    data["coordination"] = copy.deepcopy(coordination)

    ingress = data.get("ingress", INGRESS_DEFAULTS)
    if not isinstance(ingress, dict) or set(ingress) != set(INGRESS_DEFAULTS):
        raise ConfigError("ingress keys do not match schema")
    lookback = ingress["poll_lookback_seconds"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 120 <= lookback <= 3600:
        raise ConfigError("ingress poll_lookback_seconds must be between 120 and 3600")
    data["ingress"] = copy.deepcopy(ingress)

    mail = data.get("mail", MAIL_DEFAULTS)
    if not isinstance(mail, dict) or set(mail) != set(MAIL_DEFAULTS):
        raise ConfigError("mail keys do not match schema")
    share_chat_id = mail["summary_share_chat_id"]
    if share_chat_id is not None and (
        not isinstance(share_chat_id, str) or not share_chat_id.strip()
    ):
        raise ConfigError("mail summary_share_chat_id must be null or a non-empty string")
    limits = {
        "max_important_per_summary": (1, 20),
        "max_body_chars_per_message": (1000, 20000),
        "max_input_chars": (10000, 500000),
    }
    for key, (lower, upper) in limits.items():
        value = mail[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ConfigError(f"mail {key} must be between {lower} and {upper}")
    data["mail"] = copy.deepcopy(mail)

    policy = _require_mapping(data, "policy")
    draft_days = policy.setdefault('captured_draft_retention_days', None)
    if draft_days is not None and (type(draft_days) is not int or not 1 <= draft_days <= 3650):
        raise ConfigError('policy.captured_draft_retention_days must be null or an integer in 1..3650')
    body_days = policy.setdefault('body_retention_days', None)
    if body_days is not None and (type(body_days) is not int or not 1 <= body_days <= 3650):
        raise ConfigError('policy.body_retention_days must be null or an integer in 1..3650')
    for key, default, minimum, maximum in (
        ('backup_keep_recent', 14, 1, 3650), ('backup_keep_weekly', 8, 0, 520)
    ):
        value = policy.setdefault(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigError(f'policy.{key} must be an integer in {minimum}..{maximum}')
    age = policy.setdefault('backup_max_age_days', None)
    if age is not None and (type(age) is not int or not 1 <= age <= 3650):
        raise ConfigError('policy.backup_max_age_days must be null or an integer in 1..3650')
    required_policy = {
        "captured_draft_retention_days",
        "body_retention_days",
        "backup_max_age_days",
        "backup_keep_recent",
        "backup_keep_weekly",
        "auto_reply_confidence",
        "raw_retention_days",
        "board_alias",
        "board_lease_max_minutes",
        "push_approval_minutes",
    }
    if set(policy) != required_policy:
        raise ConfigError("policy keys do not match schema")
    threshold = policy["auto_reply_confidence"]
    if not isinstance(threshold, (int, float)) or not 0.85 <= threshold <= 1:
        raise ConfigError("auto_reply_confidence must be between 0.85 and 1")
    if not isinstance(policy["board_alias"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", policy["board_alias"]):
        raise ConfigError("board_alias must be a safe configured board name")
    if features["board"] and policy["board_alias"] != "board1":
        raise ConfigError("enabled board executor currently supports only board1")

    from .coding_catalog import validate as validate_coding_catalog
    try:
        data["coding_executors"] = copy.deepcopy(validate_coding_catalog(data.get("coding_executors", {})))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    identity = _require_mapping(data, "identity")
    expected_identity = {
        "telegram_control_user_id",
        "telegram_control_chat_id",
        "feishu_owner_open_id",
        "feishu_p0_chat_id",
    }
    if not expected_identity <= set(identity) or set(identity) - expected_identity - {"control_operator_id", "feishu_control_user_id", "feishu_control_chat_id"}:
        raise ConfigError("identity keys do not match schema")
    if "control_operator_id" in identity and (
        not isinstance(identity["control_operator_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", identity["control_operator_id"])
    ):
        raise ConfigError("control_operator_id must be a stable nonempty identity")
    if {"feishu_control_user_id", "feishu_control_chat_id"} & set(identity):
        for name in ("control_operator_id", "feishu_control_user_id", "feishu_control_chat_id"):
            if not isinstance(identity.get(name), str) or not identity[name] or len(identity[name]) > 256:
                raise ConfigError("Feishu control requires a principal, user ID and chat ID")
    notices = data.get("operator_notifications", {"channel": "telegram"})
    if (not isinstance(notices, dict) or set(notices) != {"channel"}
            or notices["channel"] not in ("telegram", "feishu", "web")):
        raise ConfigError("operator_notifications requires channel telegram, feishu or web")
    if notices["channel"] == "feishu" and not all(
        identity.get(key) for key in
        ("control_operator_id", "feishu_control_user_id", "feishu_control_chat_id")
    ):
        raise ConfigError("Feishu notifications require configured Feishu control identities")
    data["operator_notifications"] = copy.deepcopy(notices)
    legacy_control = all(
        isinstance(identity[name], (str, int)) and not isinstance(identity[name], bool)
        and bool(str(identity[name]).strip())
        for name in ("telegram_control_user_id", "telegram_control_chat_id")
    )
    if (features["board"] or features["wip_push"] or features["calendar"]) and not (
        identity.get("control_operator_id") or legacy_control
    ):
        raise ConfigError("control features require a stable operator or stable Telegram user and chat IDs")
    if features["auto_faq"] and identity["feishu_owner_open_id"] is None:
        raise ConfigError("auto_faq requires feishu_owner_open_id")

    scope = _require_mapping(data, "scope")
    if set(scope) != {"technical_chat_ids", "auto_reply_chat_ids"}:
        raise ConfigError("scope keys do not match schema")
    if not all(isinstance(scope[key], list) for key in scope):
        raise ConfigError("scope values must be lists")

    repositories = _require_mapping(data, "repositories")
    for name, repository in repositories.items():
        if not isinstance(name, str) or not isinstance(repository, dict):
            raise ConfigError("repositories must map names to configuration objects")
        required_repo = {"path", "remote", "base_branch"}
        optional_repo = {"build_component", "routing_keywords"}
        if not required_repo <= set(repository) or set(repository) - required_repo - optional_repo:
            raise ConfigError(f"repository {name} keys do not match schema")
        from .repository_routing import default_keywords

        keywords = repository.setdefault(
            "routing_keywords", default_keywords(name, data["schema_version"])
        )
        if (
            not isinstance(keywords, list) or len(keywords) > 32
            or any(not isinstance(term, str) or not 1 <= len(term) <= 80
                   or term != term.strip() or any(ord(char) < 32 for char in term)
                   for term in keywords)
        ):
            raise ConfigError(f"repository {name} routing_keywords must be up to 32 nonempty terms")
        if len({term.casefold() for term in keywords}) != len(keywords):
            raise ConfigError(f"repository {name} routing_keywords must be unique")
        repo_path = repository["path"]
        repo_posix = PurePosixPath(repo_path) if isinstance(repo_path, str) else None
        if (
            repo_posix is None
            or not repo_path.startswith("/")
            or source_root not in repo_posix.parents
            or any(
                not SAFE_POSIX_COMPONENT.fullmatch(part)
                for part in repo_posix.parts[1:]
            )
        ):
            raise ConfigError(f"repository {name} must be under remote_source_root")
        if ".." in repo_posix.parts:
            raise ConfigError(f"repository {name} path contains traversal")
        if not all(isinstance(repository[key], str) and repository[key] for key in required_repo):
            raise ConfigError(f"repository {name} has empty required values")
    base = _require_mapping(data, "base")
    expected_base = {
        "app_token",
        "cases_table_id",
        "knowledge_table_id",
        "mail_table_id",
        "health_table_id",
    }
    if set(base) != expected_base:
        raise ConfigError("base keys do not match schema")
    if features["base_sync"] and any(base[key] is None for key in expected_base):
        raise ConfigError("base_sync requires all Base IDs")
    return data


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return Config(validate_config(data), config_path)
