---
schema_version: 2
id: example.component.topic
revision: 1
kind: procedure
status: structured
title: Replace with a user-facing title
owner: null
scope:
  product: example-product
  component: software
  subcomponent: null
  basis: hardware_specific
  boards: [example-board]
  hardware_revisions: [explicit-revision]
  software_versions: [exact-commit-or-release]
  boot_stages: []
  storage_media: []
  operating_systems: []
intent:
  aliases: [replace-with-natural-paraphrase]
  question_examples: [Replace with a real user question]
  required_entities: []
  negative_constraints: []
content:
  summary: Replace with a concise reusable conclusion.
  prerequisites: [State what must already be true.]
  steps: [Use the least risky step first.]
  expected_observations: [State the observable success signal.]
  failure_branches: [State what a mismatch means.]
  rollback: [State how to return to the initial state.]
  warnings: []
  commands: []
claims:
  - id: main
    statement: Replace with one independently verifiable claim.
    source_refs: [source-main]
    risk_class: read_only
    required_validation: [static]
sources:
  - id: source-main
    type: git
    stable_external_id: repository:branch:path-or-symbol
    title: Source title
    url: null
    version: exact-commit
    snapshot_digest: null
    authority: 0.95
    visibility: internal
    share_mode: full_answer
    locator:
      repository: repository
      commit: exact-commit
      path: path/to/file
      symbol: symbol-name
validation:
  - id: static-review
    claim_refs: [main]
    layer: static
    result: passed
    environment:
      repository: repository
      commit: exact-commit
    artifact_digest: null
    case_id: null
    observed_at: 2026-09-03T00:00:00+08:00
publication:
  answer_visibility: internal
  source_body_visibility: internal
  link_policy: direct
  automatic_reply: false
  allowed_chat_ids: []
  allowed_user_ids: []
quality: null
review: null
---

Write the concise answer that may be sent to a colleague. Keep internal analysis,
private paths, credentials, and unsupported conclusions out of this body.
