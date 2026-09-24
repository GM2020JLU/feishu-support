# Software-only local quickstart

This starts a new, private, loopback-only instance. It needs Python 3.12+, a
browser, and internet access to install Python dependencies. It needs no Feishu
login, Telegram, remote build host, model, or development board. Run these
commands from a fresh checkout on a Linux machine; do not point them at an
existing instance or database.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install .
umask 077
mkdir -p "$HOME/.config/k3-support"
cp config/local-software-project.example.yaml "$HOME/.config/k3-support/config.yaml"
.venv/bin/k3-supportctl --config "$HOME/.config/k3-support/config.yaml" init-db
.venv/bin/k3-support-gui --config "$HOME/.config/k3-support/config.yaml" \
  --token-file "$HOME/.local/share/software-support/gui.key" --init-token
.venv/bin/k3-support-gui --config "$HOME/.config/k3-support/config.yaml" \
  --token-file "$HOME/.local/share/software-support/gui.key" --port 8765
```

Open `http://127.0.0.1:8765/` in a browser on the same machine. Read the key
from `~/.local/share/software-support/gui.key` **locally** and paste it into the
login form. Do not place it in a URL, screenshot, repository, or chat. Stop the
foreground server with Ctrl-C.

The fresh dashboard is empty by design. To view one **synthetic local Case**,
run this in another terminal, then refresh the workbench:

```sh
.venv/bin/k3-supportctl --config "$HOME/.config/k3-support/config.yaml" \
  create-case --title 'DEMO ONLY: local software bug' --type bug \
  --severity P3 --confidence 1 --idempotency-key public-demo-case-v1
```

This Case is not a Feishu Project Bug, has no remote identity, and cannot be
used as evidence of a repaired defect. Repeating the command with the same
idempotency key does not make another Case. The `Bug 研发` page remains empty
until an authorized Project Bug is created or imported.

The example configuration selects `shadow`, web-only notices, no repositories,
and all external/executor features off. It does not install system services or
start background workers. The GUI binds only `127.0.0.1`; do not expose the
port through a public proxy. Configuration and database stay under your home
directory and are not part of the Git checkout.

If startup fails, check the Python version, ensure the login key path does not
already exist before `--init-token`, and run `k3-supportctl --config ... health`
against the same configuration. Do not fix a failed start by pointing the
example at somebody else's database. See [Project setup](feishu-project-setup.md)
only after this local run works.
