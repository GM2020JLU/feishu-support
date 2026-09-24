# Office workflows

## Feishu and knowledge

Deduplicate by immutable source identity and message/event ID. Personal-message
polling overlaps the real-time bot stream; one Case and one Outbox action remain
authoritative. The polling path uses read-only message search/get APIs and does
not mark a conversation as read. Re-query the configured overlap window and
deduplicate by durable message ID so delayed search indexing cannot create a
gap. An AI acknowledgement or reply is a separate, explicitly marked outbound
action; never use read state as proof that either the operator or AI handled a
Case. Retrieved documents and chat excerpts default to
unknown requester access. Promote only reviewed, scoped, versioned answers to
approved knowledge; link duplicate questions to the canonical entry.

`knowledge-source-register` records only a stable source coordinate, version,
and ACL; registration is not evidence and grants no disclosure permission.
Attach a registered coordinate only to candidate knowledge with
`knowledge-source-attach`. The candidate disclosure must be no broader than the
source, and sources cannot be added after approval without returning the entry
to candidate review.

A historical chat backfill is read-only and owner-authorized. Archive raw chat
and thread data only in a private local directory, with resumable per-chat
checkpoints and content hashes. Extracted answers remain `private/candidate`,
carry stable message coordinates, and never enter automatic retrieval until the
operator confirms the conclusion, version scope, disclosure class, and merge
decision. A source crawl is not model training and is not approval.

An authored-document backfill is discovery, not knowledge synthesis. Search all
Doc/Docx/Wiki resources whose original creator is the operator, paginate to
completion, and fetch only document-backed Wiki nodes. Preserve stable source
coordinates, revision IDs, content hashes, URLs, ACLs, and private raw archives.
Do not infer that Sheets/Base nodes are documents or that the operator may
disclose a source body merely because they can read it. Personal debug notes may
be private investigation evidence but must never be promoted into standard
answers by summarization. Build an explicit allowlist of operational guides instead;
each route maps reviewed question variants to one fixed reply containing only
the document title and URL. Operator-authored documents use `author_share` and
can be sent directly. Other documents use `request_if_denied`; send the reviewed
link and tell a recipient without access to request it on the document page.
This permits link disclosure, never disclosure of inaccessible body content.
Keep routes as `internal/candidate` until operator review. A changed source
revision, intent set, or access mode creates a new review candidate rather than
silently mutating an approved route.

When a registered source revision, content digest, URL, ACL, or document access
mode changes, immediately mark every attached approved entry stale. An
incomplete refresh must preserve the last known version instead of erasing the
comparison point. Do not retrieve it again until the updated source and answer
are reviewed.

Question variants are semantic examples, not an exact phrase list. In the
worker path, ask the model to select at most one ID from the disclosure-filtered
approved catalog; lexical retrieval is only an offline compatibility path when
no selector is configured. The model never writes the answer: after
selection, deterministically revalidate that the ID exists, is still approved,
is visible to the requester, is within its review deadline, and clears the
configured confidence and source-authority gates. Require the selector to
abstain when target or version is ambiguous. Reject invented IDs, invalid JSON,
timeouts, and model errors; continue with the investigation path.

Before knowledge selection, use the semantic router to propose one of the seven
support paths documented in `audience-routing.md`. A verified requester profile
comes only from an operator override or allowed Contact reporting-line/job
fields; message tone is never identity evidence. Unknown identity is a safe
profile, not a reason to guess. Clarification is a one-question exception that
needs a second independent semantic review; otherwise research or debug without
burdening the requester.

Use `shadow-report --days 7` for content-free rollout evidence. Suggestion
precision is based only on stable-identity `shadow-review` decisions; unlabeled
suggestions remain outside the denominator and block the precision gate from
passing with no reviewed sample.

Treat rapid same-sender P2P fragments as one conversation Case. The worker
attaches continuations received within five minutes and suppresses a second
acknowledgment; explicit new-topic wording and all group messages remain
separate. If a duplicate Case already exists, use the audited local
`merge-case` operation instead of deleting either Case.

Before rollout, run `office-doctor`; interpret user polling/Mail, P0 routing,
enabled P0 urgent grants, Base mirror, and Base canary cleanup as independent
gates. Disabled urgent channels require neither scopes nor delivery attempts.
The doctor reads tenant scope status and uses a Base delete dry-run; it must not
send an urgent or delete a record. Use `scope-candidates` only to produce a
metadata-only review list. Never auto-add a group from its name or activity;
the operator selects `technical_chat_ids`. P2P polling remains enabled when the
group allowlist is empty.

## Mail

Use the operator's Feishu Mail identity. Important messages may trigger a
real-time alert; summaries run at 12:00 and 18:00 in the configured timezone.
Fetch bounded plain text for private summarization but never forward the raw
batch or enumerate every subject. The model produces a compact overview,
exclusive category counts and category summaries, plus an important subset,
concrete actions and explicit deadlines only when supported by content.
Categories distinguish build/CI, code review, upstream, company affairs,
project/release, support bugs, meetings, security/account, external, and other
mail. Validate every selected message ID and require category counts to cover
the exact input batch. Invalid JSON, invented IDs, timeout, or model failure
sends nothing and does not advance the delivered watermark.

Build the historical private mail catalog with bounded 100-message pages and a
durable continuation token. Read bounded plaintext only for classification;
persist sender/subject/thread metadata and the category, origin, attention, and
technical-topic dimensions, but never persist the body. A page advances only
after every exact message ID is returned and classified. Resume a failed page
without skipping it. Retry invalid AI batches as exact singletons. After three
schema-only singleton failures, retain the message as a zero-confidence
`ai_unresolved_v1` item requiring review; never invent a confident category or
silently discard it. Use `mail-catalog-query --needs-review` for this queue.
Once the Inbox and Archive run is complete, stop; realtime
ingress handles new mail. Use the catalog to answer category follow-ups, then
fetch exact message bodies only on demand.

For each important message, share the original email as a native Mail card only
to `summary_share_chat_id`, then retrieve the shared IM message's HTTPS AppLink
and include it in the Telegram digest. Mail sharing is non-idempotent: an
uncertain attempt is never automatically repeated. AppLink lookup is read-only
and may retry without sharing the email again. Only the delivered Telegram
digest advances the watermark. Do not request the reviewed mail-address
event-field scope when polling and immutable message IDs already satisfy
ingestion. Draft/send mail is a distinct write.

## Meetings

Read availability and propose title, timezone-aware time, attendees, room or
video link, and agenda. Create only from an exact operator command or an
approved digest-bound preview. Verify returned event and attendee results.

## Version impact

Accept only a configured repository, exact Change ID and hexadecimal revision,
repository-relative changed paths, subject, and branch. Assess impact against
the supplied approved knowledge catalog. Never invent a knowledge ID or infer a
different revision. Persist the input digest and notify the operator at most
once. The alert may recommend knowledge review and validation, but must not
modify knowledge or contact colleagues automatically.

## Notifications and takeover

P0 means evidenced widespread outage, security/data-loss hazard, or critical
business stop without workaround. Attempt only the notification channels enabled
in configuration, independently and with idempotency and bounded retries. Record
each receipt or failure. The current safe default keeps app urgent and SMS
urgent disabled. Format the ordinary Feishu owner reminder as restrained rich
text: one heading, then bold labels for status, Case, source, and title. Keep
approval and takeover controls in Telegram; the Feishu reminder is informational.

For ordinary Feishu support, create one conversation Turn per inbound message.
Delay public AI writes by the configured work/off-hours grace period. Bind the
Outbox row to the Turn revision and fencing token, then refresh the exact chat
and revalidate immediately before sending. A direct operator reply or `OnIt`
reaction cancels the stale write. An unthreaded same-chat operator message is a
soft hold, never proof that a particular answer was sent. Holds do not
auto-resume; only explicit `交给 AI` restores AI reply authority.

On takeover, stop scheduling and active writers, preserve Case state, artifacts,
worktrees, branches, and commits, attempt board cleanup under the existing
lease, and report the exact handoff point. Never delete evidence as cleanup.
Late retrieval or Codex results must fail their lease/Case fence and cannot
overwrite the handoff. After a worker crash, retry only read-only retrieval and
idempotent Base mirroring within their attempt budgets; never automatically
replay Codex, board, or WIP-push effects.
