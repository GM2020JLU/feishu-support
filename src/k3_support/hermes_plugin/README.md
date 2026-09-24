# K3 Support Control Hermes Plugin

This plugin intercepts `/feishu` and unmistakable Telegram control messages before Hermes
dispatches them to an LLM. It forwards the platform-provided stable user, chat,
and message IDs plus the exact command text to `k3-supportctl control`, sends a
deterministic receipt, and skips normal agent dispatch.

It also sends approval requests and the global control panel with compact
Inline Keyboards, and intercepts the `k3a:`, `k3c:`, and `fsc:` callback
namespaces. Button clicks go to
`k3-supportctl control-callback` with the original prompt message ID; the
control plane remains authoritative for identity, prompt binding, expiry,
digest, idempotency, and continuation.

The deployed plugin reads
`$HERMES_HOME/k3-support/control-plugin.json` (or the absolute path in
`K3_SUPPORT_CONTROL_PLUGIN_CONFIG`). The file schema is:

```json
{
  "schema_version": 1,
  "control_cli": "/absolute/path/to/k3-supportctl",
  "control_config": "/absolute/path/to/k3-support/config.yaml",
  "timeout_seconds": 10
}
```

Use the repository installer rather than copying files manually. The plugin has
no third-party Python dependency and never invokes a shell.

Feishu text controls use the same CLI with an explicit channel. Compatible Hermes
Feishu adapters can render mode and approval responses as Card 2.0. The plugin
consumes its own card namespace before model dispatch, requires native callback
context, and keeps the event token separate from the card message used for replies.
Adapters without card transport retain text receipts. Card delivery failure does
not imply the underlying control operation failed.

Card buttons carry server-issued card/action identifiers. The control database
binds them to the confirmed sent message, native user, chat, and 30-minute expiry.
Unbound cards and copied buttons on another message cannot execute. Real Feishu
client verification remains required before production deployment. Native SDK
callback registration must be enabled for the app.
