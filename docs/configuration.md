# Configuration and local data

Copy [`config/local-software-project.example.yaml`](../config/local-software-project.example.yaml)
to a private file as described in the [quickstart](quickstart.md). The sample is
the supported starting point for a local software demonstration. Keep `mode:
shadow`, `operator_notifications.channel: web`, empty `repositories`, and the
disabled `features` values for the first run. The larger
`config/config.example.yaml` documents additional control-plane settings and
is not the software quickstart profile.

The sample stores SQLite at `~/.local/share/software-support/state/support.db`.
The console login key is a separate owner-only file at
`~/.local/share/software-support/gui.key`. A private configuration file can
later contain space identifiers and account-specific paths. Protect all three,
including backups, and do not copy them into the source tree or an issue.

The console accepts connections only on `127.0.0.1` and requires a login key.
It is intended for one trusted local operator. Avoid port forwarding or a
public reverse proxy. Starting the console does not start the Project refresh
worker, coding agents, notification workers or a system service.

To connect a real space, follow [Feishu Project setup](feishu-project-setup.md).
Adding a reader alone does not enable writes. Project writes require active
mode, `project_integration.write_enabled: true`, a configured writer, and the
operation's own grant, review and audit checks. Do not turn on active mode or
copy a writer configuration just to populate the demo. Read-only Project
output and downloaded attachments can contain private business data.

For upgrades, stop the local console, back up the private database and key,
install the new wheel into a fresh virtual environment, then run `health`
with the same private configuration. The console applies database migrations
at start. Do not point a new or experimental build at an existing production
database for a trial run.
