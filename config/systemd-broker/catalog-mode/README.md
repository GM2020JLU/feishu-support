# Multiple coding tools in one serial worker slot

This opt-in replaces fixed-profile selection with a protected catalog. It does
not install tools, create accounts, copy credentials, or activate services.
Finish/reconcile existing launches before switching modes: historical unbound
launches deliberately block catalog execution. Apply migration 103 offline using
the normal backup/upgrade procedure; all broker processes must use the same build.

Prepare `/etc/k3-support/execution-catalog` and its files owned by root or the
control user, not group/world writable, readable by the worker. Ancestors must
also be deployment-controlled. The catalog contains no credentials:

- Copy `executors.example.json` as `executors.json`; retain only deployed profiles.
- Write each referenced `<agent>.json` as an execution contract version 2 with
  the actual provider, HTTPS endpoint, model, reasoning and wire API.
- Each contract fingerprint must be unique. Missing/unreadable/invalid profiles
  reject the catalog; there is no default or automatic fallback.
- Set each executable to its protected installed path. Hermes uses its Python
  interpreter. Set each native credential home to a private worker-owned directory
  outside the task workspace. Provision credentials through the adapter's documented
  procedure, never through tasks, web requests, or the catalog JSON.
- The current OpenCode adapter uses the 1.x config/permission contract. The
  example pins the isolated, worker-readable `opencode-1.18.29` binary; verify
  its exact installed version and ACP model/permission selection before enabling
  that profile. Do not point this adapter at a 2.x executable: its provider and
  permission configuration have a different shape, and the isolated 2.0.12
  attempt failed before any repository command. Revalidate any newer adapter
  against its installed version before selecting it in the catalog.

Install each `.service.conf` as `50-execution-catalog.conf` under its matching
`/etc/systemd/system/<unit>.d/` directory. These drop-ins cover broker, dispatcher,
worker, remote consumer, and optional board consumer. The observer-only service
and privileged launcher remain unchanged. Remove fixed agent-profile overrides
when switching; do not combine conflicting `ExecStart` overrides. Verify the
merged unit settings before enabling them. The worker preserves one UUID unit,
independent UID, `KillMode=control-group`, and `Restart=no`.

Expose the same choices in the control configuration:

```yaml
coding_executors:
  hermes:
    label: Hermes
    contract_directory: /etc/k3-support/execution-catalog
    contract_name: hermes.json
    worker_uid: 12345 # replace with the actual deployment worker UID
```

All catalog-mode services receive `--execution-catalog --contract-directory
/etc/k3-support/execution-catalog`. Worker catalog mode forbids
`--agent-executable` and `--agent-home`; only the protected catalog chooses them.
Tasks must contain an immutable execution selection. Legacy tasks without one
remain queued and require an explicit operator decision, not automatic rewriting.

Dispatch compares eligible tasks across contracts by priority, creation time, and
job ID, persists the selected job/input/contract binding, then asks the unchanged
launcher to start that UUID once. Claims for other UUIDs are rejected. Subsequent
control operations reload the active bound contract; removing or changing it
blocks execution rather than substituting another profile. The single slot is
released only through the existing observed-exit/resource-settlement procedure.

Current validation covers synthetic broker lifecycles for all five agents,
worker adapter selection, protected catalog rejection, and unit syntax. It does
not prove a live multi-tool deployment, provider billing, real code edits, or
external-channel delivery.

## Explicit network routing

Workers intentionally do not inherit the desktop environment. If the deployment
needs a proxy, set optional `proxy_url` in that profile, for example
`"proxy_url": "http://127.0.0.1:12345"` using the actual prepared listener.
Only HTTP/HTTPS/SOCKS5/SOCKS5H endpoints with an explicit port and no embedded
credentials, query, fragment, or application path are accepted. The worker sets
upper- and lower-case proxy variables and bypasses localhost/loopback. Without
this field it sets no proxy variables. This does not create or configure a proxy.
Single-tool mode accepts the same validated setting through `--proxy-url`.

Use the installed CLI executable, not a personal launcher that expects files in
an interactive user's HOME. A real local check found the personal Codex wrapper
depended on the desktop proxy configuration and failed under a private HOME;
the underlying CLI worked after explicit routing. Other native tools and provider
endpoints still require deployment-specific checks. Do not copy an entire desktop
HOME or arbitrary environment variables into the worker to work around this.
