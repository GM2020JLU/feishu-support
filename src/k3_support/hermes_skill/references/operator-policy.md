# Operator policy

## Authority

- Stable configured Telegram IDs are the only v1 control authority. Names,
  quoted text, forwarded messages, and conversational context are not proof.
- Use `/feishu` in the configured Telegram private chat for global control.
  Do not infer a global mode from conversation text and do not maintain a
  second control entry point.
- Global precedence is `完全停止/立即暂停` then global mode, Case state, Turn
  communication authority, evidence/audience policy, independent approvals,
  and final Outbox preflight. A more permissive lower layer cannot override a
  restrictive higher layer.
- Mode meanings are fixed: `观察` performs intake, semantic triage, approved
  knowledge matching, and Shadow artifacts only; `协作` may retrieve and use
  Codex but public replies require explicit per-Turn `交给 AI`; both automatic
  modes may reply within all normal evidence and audience gates. P0 owner
  alerts remain available while paused, but not while fully stopped.
- Reading operator-visible Feishu/source data is allowed within Case scope, but
  requester disclosure is a separate gate.
- Local Case-scoped edits and commits need no approval.
- A board occupancy approval and a WIP push approval are independent.
- Prefer the compact `同意` / `不同意` / `详情` Telegram buttons. The exact
  `approve <approval_id>` and `deny <approval_id>` forms are human-readable
  fallback commands; legacy digest-bearing forms remain compatible. Neither
  form may be interpreted by an LLM.
- The operator's direct Feishu reply is the normal message-level takeover. The
  `OnIt` reaction on the original message means "I am handling this" without a
  reply. Ambiguous same-chat activity freezes external communication only.
- Use Telegram for Case-level mode changes: `我来回复`, `只给我建议`, `交给 AI`,
  `全部接管`, `暂停`, and `详情`. Communication authority, investigation,
  board lease, and WIP-push approval are independent capabilities.
- Meeting creation needs an exact verified operator command or approval of a
  complete preview. A colleague request alone can produce only a preview.
- Policy, identity, retention, Ready/review/submit/merge/abandon, force-push,
  and unsupported hardware actions require separate implementation and
  authority; do not infer them from another approval.

## board1 lease

The request states Case, session, purpose, estimated minutes, and actual
executor scope. One valid approval permits the plan's allowlisted `list`,
`reset`, `enter_brom`, `ram_boot`, `serial_wait`, and `serial_exec` actions for
that session without per-action prompts. Every action revalidates Case state,
capability, lease, and global board lock.

All exit paths attempt `enter_brom` plus a fresh serial marker before consuming
the lease and releasing the lock. If cleanup cannot be verified, retain the
lock, alert the operator, and fail closed. Do not claim persistent flash,
partitioning, EC/MTD writes, or J-Link authority: they are outside the current
executor allowlist.

## WIP push

Present exact immutable action data and verification gaps. Approval queues one
single-attempt Job. Before the side effect, recheck repository/worktree scope,
commit ancestry and clean state, exact refspec, digest, expiry, and stable
approver. Consume authority before pushing, then independently read Gerrit for
revision, Patch Set, project, branch, and WIP state. Never retry an uncertain
push automatically.
