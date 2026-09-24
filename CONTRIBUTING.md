# Contributing

Use Python 3.12 or newer. Create a virtual environment, install the project
with its test extra (`python -m pip install -e '.[test]'`), and run focused tests
for the behavior you changed. Run the wider suite at an integration/release
boundary or when a concrete cross-module risk justifies it. Check formatting
with Ruff where available and run `git diff --check` before proposing a change.

Keep business writes behind the control layer's identity, grant, audit,
idempotency and approval checks. Test doubles can exercise failures but do not
prove real Feishu Project or device acceptance. Document the exact proof level
of any new feature.

Never commit credentials, OAuth profiles, databases, logs, private Bug content,
customer material, model weights, runtime directories or generated virtual
environments. Use synthetic fixtures and redact logs before sharing them.
Changes to third-party knowledge content need provenance and redistribution
rights recorded before inclusion.

Contributions to this distribution are made under Apache-2.0. Submit only
material you have the right to contribute; keep internal knowledge articles
and environment-specific acceptance evidence out of public changes.
