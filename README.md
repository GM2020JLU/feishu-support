# K3 Support: Feishu Project engineering assistant

This Python project provides a local control plane and web console for engineering
support work. It can track a Bug through intake, investigation, code execution,
verification review, controlled Project writeback, and closure. The software Bug
path has been exercised in an **isolated acceptance instance**; that result is
not a production deployment, device validation, or proof that every coding
tool and chat entry point works at the same depth.

The example configuration contains no Feishu credentials. A new checkout starts
in a local, web-only `shadow` mode. It cannot read or change anyone's Feishu Project
until the operator configures an official client identity, an allowed space and
type, and the required control-layer permissions.

## Try it locally

Python 3.12 or newer is required. Follow the [software-only quickstart](docs/quickstart.md)
to install in a virtual environment, create a private database and login key,
open the console at `http://127.0.0.1:8765/`, and view a clearly synthetic
local Case. The first start needs no device, remote build host, Hermes, model,
Telegram, or Feishu login. It does not create a remote Bug.

## What is available

| Area | Current boundary |
| --- | --- |
| Local control | SQLite state, jobs, approvals, audit records, and a loopback-only token-authenticated console. |
| Feishu Project | Official-client based reads and controlled create, comment, field-write, transition, and close paths; real use needs instance-specific schema, permissions, grants, and review. |
| Software investigation | Repository binding, isolated workspaces, coding jobs, commit/test evidence, and independent human review. |
| Hardware validation | Device identity and evidence gates exist; a general public device workflow is not delivered. |
| Other entries and tools | Feishu/Telegram control and five coding adapters have different acceptance depths; do not assume parity from one software Bug acceptance. |

Execution success, repair review, verification approval, and remote Bug closure
are separate facts. A build or model report alone does not prove a fix.

## Configure a real installation

Start from `config/local-software-project.example.yaml`. Keep the initial web
profile isolated while configuring repositories, an authorized official Feishu
Project client, and the exact spaces and Bug types you may access. Project
writes stay disabled by default. See [Project setup](docs/feishu-project-setup.md)
for the prerequisite discovery and authorization boundary. Do not copy another operator's
IDs, database, login key, OAuth state, or runtime directories.

The wheel is the supported deployment artifact. Do not copy a development
virtual environment into a service. No service is installed or started by the
quickstart.

## Development and contributing

See [contributing](CONTRIBUTING.md) for the development setup and focused
verification rules. Security issues should follow [SECURITY.md](SECURITY.md).
Historical acceptance reports are environment-specific evidence with stated
limits, not a setup guide for a new user.

This curated source distribution is licensed under
[Apache-2.0](LICENSE). It excludes the internal knowledge article corpus,
runtime data, credentials, and environment-specific acceptance records. The
older `feishu-support` repository history is separate from this distribution.
