-- Bound child lookup work to the requested canonical group, not all Cases.
CREATE INDEX idx_cases_canonical_group ON cases(canonical_case_id,case_id);
