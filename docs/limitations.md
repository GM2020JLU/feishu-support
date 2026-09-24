# Feature and acceptance limits

The quickstart creates one synthetic **local Case**. It is visible on the
workbench, but it is not a Feishu Project Bug, does not appear in the `Bug 研发`
list, and proves no remote authorization or writeback.

The control plane includes guarded Project read, Bug creation, comment, field
write, transition and close code paths. An operator must configure the official
client, discover their current space schema and item permissions, and opt into
each write path. A request accepted locally is not proof that a remote change
was applied; read the exact remote item after a write. Unknown outcomes require
reconciliation before retrying.

The isolated software Bug acceptance exercised a particular setup and review
flow. It does not certify another tenant, a production service, all five
coding adapters, chat and mail entry points, or a device. Compilation and
unit tests do not prove board behavior. The optional knowledge corpus,
embeddings, model weights, external services and runtime data are not shipped.
The default local installation makes no automatic Feishu reply.
