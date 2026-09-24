"""Shared deterministic Bug controls after channel authentication.

The HTTP gateway supplies authentication and CSRF; later chat gateways must use
their authenticated operator mapping. There is intentionally no adapter selection,
credential, snapshot ingestion or direct remote dispatch in this public surface.
"""

import json

from . import project_bug_grants as grants
from . import project_bug_operations as operations
from . import project_bugs, project_verification, project_verification_runs
from .project_bugs import _text
from .project_investigation import CONTINUATION_FIELDS
from .project_investigation import FIELDS as INVESTIGATION_FIELDS
from .project_repair_reviews import FIELDS as REPAIR_REVIEW_FIELDS
from .project_result_comment import FIELDS as RESULT_COMMENT_FIELDS
from .project_verification_reviews import FIELDS as VERIFICATION_REVIEW_FIELDS
from .project_verifier_job import FIELDS as VERIFIER_JOB_FIELDS

FIELDS = {
    "workflow-options": {"bug_id", "grant_id", "snapshot_id", "expected_revision"},
    "repair-review-detail": {"bug_id", "round_id"},
    "record-repair-review": REPAIR_REVIEW_FIELDS,
    "create-form": {"grant_id", "host", "project_key", "type_key"},
    "search-field-related": {"bug_id", "grant_id", "field_key", "query"},
    "search-create-related": {"grant_id", "host", "project_key", "type_key", "field_key", "query"},
    "search-create-users": {"grant_id", "host", "project_key", "type_key", "field_key", "query"},
    "search-create-duplicates": {"draft_id", "expected_digest", "keyword", "request_id"},
    "attach-create-search": {"draft_id", "expected_digest", "search_id"},
    "bind-created-draft": {"draft_id", "expected_digest", "read_hours", "local_priority"},
    "retry-created-draft-read": {"draft_id", "expected_digest", "read_hours", "local_priority", "retry_intake_id"},
    "send-comment": {"operation_id", "expected_digest", "request_id"},
    "comment-send-status": {"dispatch_id"},
    "send-write": {"operation_id", "expected_digest", "request_id"},
    "write-send-status": {"dispatch_id"},
    "request-close-approval": {"bug_id", "request_id", "change", "expires_at"},
    "decide-close-approval": {"approval_id", "request_id", "approve", "expected_digest"},
    "close-approvals": {"bug_id"},
    "close-approval-detail": {"approval_id"},
    "settle-unknown-write": {"operation_id", "verdict", "evidence_text"},
    "issue-create-grant": {"request_id", "scope", "expires_at"},
    "list-create-grants": {"after_id"},
    "create-scope-options": set(),
    "revoke-create-grant": {"grant_id"},
    "prepare-create-draft": {
        "request_id",
        "grant_id",
        "host",
        "project_key",
        "type_key",
        "field_values",
        "required_fields",
    },
    "create-drafts": {"after_id"},
    "attach-create-duplicates": {"draft_id", "search_id", "candidates"},
    "confirm-create-not-duplicate": {"draft_id", "expected_digest"},
    "mark-create-ready": {"draft_id", "expected_digest"},
    "dispatch-create-draft": {"draft_id", "expected_digest"},
    "settle-create-verified-missing": {"draft_id", "expected_digest"},
    "settle-create-verified-created": {"draft_id", "expected_digest", "created_item_id"},
    "reopen-create-draft": {"draft_id", "expected_digest"},
    "cancel-create-draft": {"draft_id", "expected_digest"},
    "comment-reconcile": {"operation_id", "write_digest", "grant_id", "request_id"},
    "write-reconcile": {"operation_id", "write_digest", "grant_id", "request_id"},
    "attachment-download": {"activity_id", "field_key", "source_digest", "grant_id", "request_id"},
    "activity-read": {"bug_id", "grant_id", "request_id", "kind"},
    "activity-status": {"activity_id"},
    "activity-page": {"activity_id", "offset"},
    "search-options": set(),
    "search": {"simple_name", "type_key", "keyword", "request_id"},
    "search-next": {"search_id", "request_id"},
    "search-status": {"search_id"},
    "verification-review-detail": {"run_id"},
    "verification-review-output": {"run_id", "evidence_digest", "channel", "offset"},
    "record-verification-review": VERIFICATION_REVIEW_FIELDS,
    "prepare-result-comment": RESULT_COMMENT_FIELDS,
    "investigation-result": {"bug_id", "job_id"},
    "create-investigation-job": INVESTIGATION_FIELDS,
    "continue-investigation-job": CONTINUATION_FIELDS,
    "create-verification-job": VERIFIER_JOB_FIELDS,
    "intake-link": {"url", "request_id", "read_hours", "local_priority"},
    "intakes": {"after_id"},
    "intake-status": {"intake_id"},
    "prepare-source-verification-run": {
        "plan_id",
        "step_id",
        "grant_id",
        "remote_request_id",
        "remote",
        "request_id",
        "source_paths",
    },
    "prepare-verification-run": {
        "plan_id",
        "step_id",
        "grant_id",
        "remote_request_id",
        "remote",
        "request_id",
    },
    "publish-verification-plan": {
        "bug_id",
        "round_id",
        "request_id",
        "expected_revision",
        "plan",
    },
    "list": {"after_id"},
    "detail": {"bug_id"},
    "refresh": {"bug_id", "grant_id", "request_id"},
    "refresh-status": {"refresh_id"},
    "start-round": {"bug_id", "request_id", "reason", "expected_revision"},
    "prepare-write": {
        "bug_id",
        "snapshot_id",
        "request_id",
        "grant_id",
        "expected_revision",
        "action",
        "change",
    },
    "cancel-write": {"operation_id", "expected_digest"},
    "list-grants": {"bug_id", "after_id"},
    "issue-grant": {"request_id", "scope", "expires_at"},
    "revoke-grant": {"grant_id"},
}


def execute(conn, config, *, action, payload):
    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Bug controls require a configured operator")
    if (
        action not in FIELDS
        or not isinstance(payload, dict)
        or set(payload) != FIELDS[action]
    ):
        raise ValueError("Bug control requires exact request fields")
    if action in {"search-create-duplicates", "attach-create-search", "attach-create-duplicates"}:
        from . import project_create_duplicates as duplicates
        if action == "search-create-duplicates":
            return duplicates.enqueue(conn, config, actor=actor, **payload)
        if action == "attach-create-duplicates":
            from .project_bug_create import _owned
            payload = payload | {"expected_digest": _owned(conn, payload["draft_id"], actor)["request_digest"]}
        from .project_bug_create import projection
        return projection(duplicates.attach(conn, config, actor=actor, **payload))
    if action == "search-field-related":
        from .project_field_related import search

        return search(conn, config, actor=actor, **payload)
    if action == "search-create-related":
        from .project_create_related import search
        return search(conn, config, actor=actor, **payload)
    if action == "search-create-users":
        from .project_create_users import search
        return search(conn, config, actor=actor, **payload)
    if action == "create-form":
        from .project_create_schema import form
        return form(conn, config, actor=actor, **payload)
    if action == "workflow-options":
        from .project_workflow_options import read

        return read(conn, config, actor=actor, **payload)
    if action in {"bind-created-draft", "retry-created-draft-read"}:
        from .project_created_intake import enqueue

        return enqueue(conn, config, actor=actor, **payload)
    if action in {"send-comment", "comment-send-status"}:
        from .project_comment_dispatch import enqueue, status

        if action == "send-comment":
            return enqueue(conn, config, actor=actor, **payload)
        return status(conn, actor=actor, **payload)
    if action in {"send-write", "write-send-status"}:
        from .project_write_dispatch import enqueue, status

        if action == "send-write":
            return enqueue(conn, config, actor=actor, **payload)
        return status(conn, actor=actor, **payload)
    if action in {"request-close-approval", "decide-close-approval", "close-approvals", "close-approval-detail"}:
        from . import project_close_gate as close_gate

        if action == "close-approval-detail":
            return close_gate.detail(conn, payload["approval_id"], config=config)
        if action == "request-close-approval":
            return close_gate.request(conn, actor=actor, config=config, **payload)
        if action == "decide-close-approval":
            return close_gate.decide(conn, actor=actor, config=config, **payload)
        _text(payload["bug_id"], "Bug ID")
        return {"items": close_gate.status_for_bug(conn, payload["bug_id"], config=config)}
    if action == "settle-unknown-write":
        from .project_unknown_settlement import settle

        return settle(conn, actor=actor, **payload)
    if action in {"issue-create-grant", "list-create-grants", "revoke-create-grant"}:
        from . import project_create_grants as create_grants

        if action == "issue-create-grant":
            return create_grants.projection(
                create_grants.issue(conn, actor=actor, **payload)
            )
        if action == "list-create-grants":
            return create_grants.list_for_actor(conn, actor=actor, **payload)
        create_grants.revoke(conn, actor=actor, **payload)
        return {"grant_id": payload["grant_id"], "status": "revoked"}
    if action == "create-scope-options":
        # This is deliberately a configuration projection, not a grant or
        # metadata read. Only identifiers already accepted for link intake are
        # exposed, so the browser never needs reader executable/profile data.
        from .project_intake_policy import validate as validate_intake_spaces
        from .project_reader_config import selected as selected_reader

        reader = selected_reader(config)
        spaces = validate_intake_spaces(
            config.raw.get("project_integration", {}).get("intake_spaces", [])
        )
        options = []
        if reader:
            for space in spaces:
                for type_key in space["type_keys"]:
                    options.append(
                        {
                            "simple_name": space["simple_name"],
                            "project_key": space["project_key"],
                            "type_key": type_key,
                        }
                    )
        return {
            "available": bool(reader and options),
            "reader_host": reader["host"] if reader else None,
            "options": options,
        }
    if action in {
        "prepare-create-draft",
        "create-drafts",
        "attach-create-duplicates",
        "confirm-create-not-duplicate",
        "mark-create-ready",
        "dispatch-create-draft",
        "settle-create-verified-missing",
        "settle-create-verified-created",
        "reopen-create-draft",
        "cancel-create-draft",
    }:
        from . import project_bug_create as bug_create

        if action == "create-drafts":
            after = payload["after_id"]
            if not isinstance(after, str) or len(after) > 256:
                raise ValueError("invalid create draft cursor")
            rows = conn.execute(
                "SELECT * FROM project_bug_create_drafts WHERE actor=? AND draft_id>? ORDER BY draft_id LIMIT 31",
                (actor, after),
            ).fetchall()
            return {
                "items": [bug_create.projection(row) for row in rows[:30]],
                "next_cursor": rows[29]["draft_id"] if len(rows) > 30 else None,
            }
        # Settlement custody (settle-created/unknown/rejected) stays out of
        # public controls; dispatch consumes it through the accepted native
        # creation transport with its own reserve/settle discipline. The only
        # exception is explicit operator settlement of an unknown result after
        # a remote recheck, which is itself audited draft custody.
        if action == "dispatch-create-draft":
            from .project_create_dispatch import send as create_send

            return bug_create.projection(
                create_send(conn, config, actor=actor, **payload)
            )
        consumer = {
            "prepare-create-draft": bug_create.prepare,
            "attach-create-duplicates": bug_create.attach_duplicates,
            "confirm-create-not-duplicate": bug_create.confirm_not_duplicate,
            "mark-create-ready": bug_create.mark_ready,
            "settle-create-verified-missing": bug_create.operator_settle_missing,
            "settle-create-verified-created": bug_create.operator_settle_found,
            "reopen-create-draft": bug_create.reopen,
            "cancel-create-draft": bug_create.cancel,
        }[action]
        return bug_create.projection(consumer(conn, actor=actor, **payload))
    if action == "comment-reconcile":
        from .project_comment_reconcile import enqueue

        return enqueue(conn, config, actor=actor, **payload)
    if action == "write-reconcile":
        from .project_write_reconcile import enqueue

        return enqueue(conn, config, actor=actor, **payload)
    if action == "attachment-download":
        from .project_attachment_download import enqueue

        return enqueue(conn, config, actor=actor, **payload)
    if action in {"activity-read", "activity-status", "activity-page"}:
        from . import project_activity

        if action == "activity-read":
            return project_activity.enqueue(conn, config, actor=actor, **payload)
        consumer = project_activity.status if action == "activity-status" else project_activity.page
        return consumer(conn, actor=actor, **payload)
    if action in {"search-options", "search", "search-next", "search-status"}:
        from . import project_bug_search as search

        if action == "search-status":
            return search.status(conn, actor=actor, **payload)
        consumer = {"search-options": search.options, "search": search.enqueue, "search-next": search.next_page}[action]
        return consumer(conn, config, actor=actor, **payload)
    if action in {"verification-review-detail", "verification-review-output", "record-verification-review"}:
        from . import project_verification_reviews as reviews

        if action == "record-verification-review":
            return reviews.record(conn, actor=actor, payload=payload)
        if action == "verification-review-output":
            return reviews.output(conn, **payload)
        return reviews.detail(conn, payload["run_id"])
    if action == "prepare-result-comment":
        from .project_result_comment import prepare

        return prepare(conn, actor=actor, payload=payload)
    if action == "investigation-result":
        from .project_investigation_result import draft

        return draft(conn, config=config, **payload)
    if action in {"repair-review-detail", "record-repair-review"}:
        from . import project_repair_reviews as repair

        if action == "record-repair-review":
            return repair.record(conn, config, actor=actor, payload=payload)
        return repair.detail(conn, config, **payload)
    if action == "list":
        after = payload["after_id"]
        if not isinstance(after, str) or len(after) > 256:
            raise ValueError("invalid Bug cursor")
        rows = conn.execute(
            """SELECT b.bug_id,b.case_id,b.host,b.project_key,b.type_key,b.item_id,
                b.revision,c.title FROM project_bugs b JOIN cases c USING(case_id)
                WHERE b.bug_id>? ORDER BY b.bug_id LIMIT 31""",
            (after,),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows[:30]],
            "next_cursor": rows[29]["bug_id"] if len(rows) > 30 else None,
        }
    if action in {"intake-link", "intakes", "intake-status"}:
        from . import project_link_intake

        if action == "intake-link":
            return project_link_intake.enqueue(conn, config, actor=actor, **payload)
        if action == "intakes":
            return project_link_intake.listing(conn, config, actor=actor, **payload)
        _text(payload["intake_id"], "intake ID")
        return project_link_intake.status(conn, actor=actor, **payload)
    if action == "create-verification-job":
        from .project_verifier_job import submit

        return submit(conn, config, payload)
    if action in {"create-investigation-job", "continue-investigation-job"}:
        from .project_investigation import submit

        return submit(conn, config, payload)
    if action == "detail":
        _text(payload["bug_id"], "Bug ID")
        result = project_bugs.detail(conn, payload["bug_id"])
        from .project_close_lifecycle import inspect as lifecycle_status
        from .project_field_writer_config import validate as validate_writer
        from .project_repair_reviews import detail as repair_detail

        writer = config.raw.get("project_integration", {}).get("field_writer")
        closing_ids = validate_writer(writer)["closing_status_ids"] if writer is not None else ()
        for round_ in result['rounds']:
            repair=repair_detail(conn,config,bug_id=result['bug_id'],round_id=round_['round_id'])
            round_['repair_state']=repair['repair_state']
            round_['repair_review_state']=repair['review_state']
            round_['lifecycle'] = (
                {"state": "archived", "reason": "historical investigation round",
                 "requires_new_round": False, "source": "local_observed_lifecycle"}
                if round_['archived_at'] is not None else lifecycle_status(
                    conn, bug_id=result['bug_id'], round_id=round_['round_id'],
                    closing_status_ids=closing_ids)
            )
        snapshot = result["snapshot"]
        rows = conn.execute(
            "SELECT * FROM project_bug_operations WHERE bug_id=? ORDER BY rowid DESC LIMIT 50",
            (payload["bug_id"],),
        ).fetchall()
        result["operations"] = []
        for row in rows:
            item = {
                key: row[key]
                for key in (
                    "operation_id",
                    "action",
                    "state",
                    "request_digest",
                    "write_digest",
                    "created_at",
                )
            }
            item["result"] = (
                json.loads(row["result_json"]) if row["result_json"] else None
            )
            item["preview"] = (
                operations.preview(conn, row["operation_id"], snapshot)
                if snapshot
                else None
            )
            item["can_reconcile"] = row["actor"] == actor and row["action"] == "bug.comment" and row["state"] in {"dispatched", "unknown"}
            if row["actor"] == actor and row["action"] == "bug.comment":
                from .project_comment_dispatch import controls as send_controls

                item["send_control"] = send_controls(conn, config, actor=actor, operation_id=row["operation_id"])
            if row["actor"] == actor and row["action"] in {"bug.fields", "bug.transition", "bug.close"}:
                from .project_write_dispatch import controls as write_send_controls

                item["send_control"] = write_send_controls(conn, config, actor=actor, operation_id=row["operation_id"])
            item["can_settle"] = row["actor"] == actor and row["state"] == "unknown"
            item["can_cancel"] = row["state"] == "prepared" and row["actor"] == actor
            result["operations"].append(item)
        from .project_investigation import projection

        result["investigation_jobs"] = projection(conn, payload["bug_id"])
        from .project_close_gate import status_for_bug

        result["close_approvals"] = status_for_bug(conn, payload["bug_id"], config=config)
        result["investigation_jobs_limit"] = 100
        result["operations_limit"] = 50
        result["snapshot_source"] = "local_cache"
        result["remote_dispatch_available"] = False
        remote_project = result["project_key"] != "synthetic-local-only"
        result["grant_controls_available"] = remote_project
        result["plan_controls_available"] = True
        if remote_project:
            from .project_activity import controls as activity_controls
            from .project_refresh import controls

            result["refresh_control"] = controls(conn, config, result, actor)
            result["activity_control"] = activity_controls(
                conn, result["bug_id"], actor, result["refresh_control"]
            )
        else:
            result["refresh_control"] = None
            result["activity_control"] = None
        result["verification_catalog"] = {
            "repositories": sorted(config.raw["repositories"]),
            "node": config.runtime("remote_host"),
        }
        result["verification_plans"] = [
            plan
            for round_ in result["rounds"]
            if (plan := project_verification.current(conn, round_["round_id"]))
            is not None
        ]
        for plan in result["verification_plans"]:
            for round_ in result["rounds"]:
                if round_["round_id"] == plan["round_id"]:
                    round_["verification_state"] = plan["verification_state"]
        return result
    if action == "refresh":
        from .project_refresh import enqueue

        return enqueue(conn, config, actor=actor, **payload)
    if action == "refresh-status":
        from .project_refresh import status

        _text(payload["refresh_id"], "refresh ID")
        return status(conn, actor=actor, **payload)
    if action == "publish-verification-plan":
        return project_verification.publish(conn, actor=actor, **payload)
    if action in {"prepare-verification-run", "prepare-source-verification-run"}:
        if (
            action == "prepare-source-verification-run"
            and payload["source_paths"] is None
        ):
            raise ValueError("source paths required")
        run = project_verification_runs.prepare(
            conn, config=config, actor=actor, **payload
        )
        return next(
            item
            for item in project_verification_runs.projection(conn, run["plan_id"])
            if item["run_id"] == run["run_id"]
        )
    if action == "start-round":
        return project_bugs.start_round(conn, actor=actor, **payload)
    if action == "list-grants":
        return grants.list_for_bug(conn, actor=actor, **payload)
    if action == "issue-grant":
        return grants.projection(grants.issue(conn, actor=actor, **payload))
    if action == "revoke-grant":
        _text(payload["grant_id"], "grant ID")
        grants.revoke(conn, actor=actor, **payload)
        return {"grant_id": payload["grant_id"], "status": "revoked"}
    if action == "prepare-write":
        return operations.prepare(conn, actor=actor, **payload)
    return operations.cancel(conn, actor=actor, **payload)
