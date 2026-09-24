CREATE TABLE project_result_comments (
 operation_id TEXT PRIMARY KEY REFERENCES project_bug_operations(operation_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 result_digest TEXT NOT NULL CHECK(length(result_digest)=64),
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64)
);
CREATE TRIGGER project_result_comments_immutable BEFORE UPDATE ON project_result_comments
BEGIN SELECT RAISE(ABORT,'result comment provenance is immutable'); END;
CREATE TRIGGER project_result_comments_retention BEFORE DELETE ON project_result_comments
BEGIN SELECT RAISE(ABORT,'result comment provenance requires controlled retention'); END;
