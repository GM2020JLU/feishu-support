# Security reports

Please do not post credentials, private Bug content, exploit steps against a
live installation, or customer data in a public issue. Use GitHub's private
vulnerability reporting for this repository if it is enabled. Otherwise open
a public issue containing only a non-sensitive request for a private reporting
channel; share technical details only after that channel is established.

If a secret may have been exposed, revoke or rotate it promptly. Removing a
file or rewriting Git history alone does not invalidate copied credentials.

The web console is designed for a trusted local machine and binds to loopback.
It is not a public multi-user web service. Project credentials belong only to
the server-side control identity; coding workers should receive scoped tasks,
not those credentials. Keep instance data, login keys and backups outside Git.
