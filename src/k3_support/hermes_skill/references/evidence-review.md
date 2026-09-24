# Evidence review

Separate evidence layers: static inspection, build, RAM boot, persistent flash,
real-device function, and stability. Never promote one layer into another.

For a Codex completion, accept only the immutable Case/job bundle produced by
the control plane. The artifact manifest must bind configured repositories,
full commit IDs, Case-root worktrees, exact check argv and exit codes, output
paths and SHA-256 hashes, plus at most one next gated action. Independent
verification of Git state and artifact hashes is required; agent prose is not
evidence.

A reply Decision must:

- reference only allowlisted verified Evidence IDs;
- match the current Case ID and version;
- use the immutable source message as destination;
- start the draft with `[AI 自动回复]`;
- state only completed evidence layers and material limitations;
- avoid internal paths, credentials, private content, and unsupported claims;
- choose `wait` or `escalate` when status is partial, blocked, failed,
  contradictory, stale, or not requester-disclosable.

Codex output is never sent directly. A reusable answer becomes a knowledge
candidate, not approved knowledge, until stable-identity operator review.
Source-registry metadata is only a discovery coordinate. It does not prove the
source content, requester ACL, freshness, or any technical claim.
