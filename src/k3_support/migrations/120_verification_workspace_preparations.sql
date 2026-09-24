-- A fixed checkout-only action precedes the separately journaled test action.
CREATE TABLE project_verification_workspaces (
 request_id TEXT PRIMARY KEY REFERENCES broker_remote_actions(request_id),
 preparation_request_id TEXT NOT NULL UNIQUE REFERENCES broker_remote_actions(request_id),
 CHECK(request_id != preparation_request_id)
);
CREATE TRIGGER verification_workspace_immutable BEFORE UPDATE ON project_verification_workspaces
BEGIN SELECT RAISE(ABORT,'verification workspace binding is immutable'); END;
CREATE TRIGGER verification_workspace_retention BEFORE DELETE ON project_verification_workspaces
BEGIN SELECT RAISE(ABORT,'verification workspace requires controlled retention'); END;
