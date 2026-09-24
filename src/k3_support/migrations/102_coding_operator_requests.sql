CREATE UNIQUE INDEX idx_jobs_coding_operator_request ON jobs(
    json_extract(context_json,'$.operator_request.actor'),
    json_extract(context_json,'$.operator_request.request_id')
) WHERE job_type='codex' AND json_valid(context_json)
    AND json_type(context_json,'$.operator_request')='object';
