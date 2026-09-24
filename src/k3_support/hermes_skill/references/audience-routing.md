# Audience-aware support routing

## Trust the profile, not the prose

Never infer hierarchy or job function from how a message is phrased. Use only a
stable operator override or verified Feishu Contact fields. A manual operator
profile is authoritative. If the Contact lookup is disabled, lacks scope, is
stale, or is ambiguous, use `unknown`; do not guess.

Organization lookup needs only reporting-line, department, and job-title
fields. It does not need a person's email address. Keep it disabled until those
specific read scopes are reviewed and granted.

## Seven routes

- `ignore`: only obvious non-work noise or a pure acknowledgement, at least
  0.95 confidence. Uncertain technical content is never ignored.
- `direct_answer`: only a current approved knowledge item that survives the
  requester disclosure, confidence, authority, and review-deadline gates.
- `clarify`: one short question for one fact that materially changes the next
  action and cannot be retrieved internally.
- `research`: documentation or history lookup is likely sufficient. Share only
  exact selected links; never summarize inaccessible document bodies.
- `codex_debug`: code, logs, source ownership, builds, reproduction, or a fix is
  needed. Use the Case-scoped Codex path.
- `owner_decision`: commitments, ETA, priority, product/policy choice, access,
  interpersonal judgment, or any socially risky uncertainty belongs to the
  operator.
- `urgent_notify`: evidenced widespread outage, security/data-loss risk, or a
  critical business stop without a workaround. Notification is internal; it is
  not permission to promise a resolution.

The model proposes a route. Deterministic validation rejects malformed output,
invented knowledge, weak confidence, unsafe ignore decisions, unsupported
commitments, and risky clarification. A model timeout or invalid result falls
back to research or Codex; it never becomes authority.

## Conversation relationship

When recent same-sender private-chat context is supplied, classify the current
message separately from its workflow route:

- `continuation`: it depends on, corrects, or adds useful context to that Case.
- `acknowledgement`: it only confirms receipt, thanks, or closes the exchange.
- `new_topic`: it raises a separate problem even if it arrived seconds later.
- `standalone`: no reliable relationship to the supplied Case is established.

Never infer `continuation` from sender, chat, or time proximity alone. The
control plane decides route and relationship before it creates a Case or a
retrieval job. High-confidence standalone noise creates no Case. A continuation
or acknowledgement may be attached to the recent Case, but only an actionable
`research`, `codex_debug`, or `urgent_notify` route may create retrieval work.
The first valid route and recent-Case candidate are durable and reused after a
crash; never ask the model to reinterpret the same event on retry. A direct
answer also binds the exact approved knowledge ID, source digest, and match
confidence. Recheck those gates on recovery and fall back to research if they
no longer hold.

## Clarification etiquette

Automatic clarification is permitted only for verified peer, direct-report, or
cross-functional engineering, QA, or operations profiles. It requires route
confidence at least 0.92 and an independent second model decision of
`necessary_and_minimal`. Send at most one clarification per Case.

Reject the question when the system can find the answer itself, when silent
research/debug can proceed, when the answer will not change the next action, or
when it requests a bundle such as all logs, all code, screenshots, environment,
and reproduction steps. Ask for one smallest discriminating fact, such as the
exact version or the first failing log marker. Mark it `[AI 助手确认]`.

Never automatically clarify with a supervisor, dotted-line supervisor, project
manager, product manager, management profile, external requester, or unknown
profile. Continue research/debug where safe; otherwise use `owner_decision`.

## Audience response strategy

- Supervisor or management: concise and respectful; lead with verified outcome,
  impact, risk, and next decision. No raw-log dump, speculative cause, automatic
  question, ETA, priority, or commitment.
- Project manager: lead with affected scope, current state, blocker, workaround,
  owner, and decision needed. Never invent a delivery date.
- Product manager: explain user impact, supported/unsupported boundary,
  workaround, evidence status, and the decision needed. Do not turn technical
  possibility into a product commitment.
- QA or validation: be collaborative and precise. Prefer version, target,
  expected versus actual behavior, minimal reproduction, and the first useful
  log marker. Do not ask for a large undirected evidence bundle.
- Engineering or operations peer: use concise technical detail, exact commands,
  versions, evidence boundaries, and reproducible next steps. Distinguish static,
  build, RAM boot, persistent flash, device function, and stability evidence.
- Direct report: teach enough to unblock without sounding supervisory unless the
  verified role requires it; never expose private evidence.
- External or unknown: use short neutral language, assume no shared context or
  access, and escalate any promise, disclosure uncertainty, or interpersonal
  judgment.

A verified external requester may receive an automatic direct answer only from
`public` knowledge. An `internal`, `team`, or `private` match is useful context
for the operator but must route to `owner_decision`; it is not reply evidence.

Every outbound colleague message keeps the AI marker. Evidence and disclosure
gates are identical for every rank; hierarchy changes tone and escalation, not
truth standards or access rights.
