# Security reports

Please do not post credentials, private Bug content, exploit steps against a
live installation, or customer data in a public issue. Use this repository's
[Security Advisories page](https://github.com/GM2020JLU/feishu-support/security/advisories)
and select **Report a vulnerability** to send a private report to the
maintainers. Include the affected
version, reproduction steps, impact, and a safe way to confirm the issue.

If a secret may have been exposed, revoke or rotate it promptly. Removing a
file or rewriting Git history alone does not invalidate copied credentials.

The web console is designed for a trusted local machine and binds to loopback.
It is not a public multi-user web service. Project credentials belong only to
the server-side control identity; coding workers should receive scoped tasks,
not those credentials. Keep instance data, login keys and backups outside Git.
