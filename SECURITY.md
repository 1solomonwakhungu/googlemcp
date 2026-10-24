# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's
**Security** tab and click **Report a vulnerability**. Do not open a public
issue, pull request, or discussion for a security problem.

Include what you found, how to reproduce it, and the version or commit you
tested. Never include real OAuth client secrets, refresh tokens, access
tokens, or data from a Google account in a report; use fake values.

You can expect an acknowledgement within 7 days. Fixes are released as a new
version and described in [CHANGELOG.md](CHANGELOG.md); reporters are
credited unless they ask not to be.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope: the `googlemcp` package and CLI, the Terraform module in
`infra/terraform`, the acceptance scripts, and the CI workflows. Examples:
a secret or token printed to output or logs, a tool exposed without the
write or destructive opt-in it requires, a network listener opened by
`googlemcp serve`, or path injection into a Google API request.

Out of scope: vulnerabilities in Google APIs or in MCP clients, a
compromised workstation or Google account, and prompt injection through
content the model reads, unless GoogleMCP bypasses its own write or
destructive opt-ins as a result.

## If one of your secrets leaks

Revoke it at the source first. For a client secret, add a new secret to the
OAuth client in the Google Auth Platform console and delete the leaked one.
For a refresh token, run `googlemcp auth revoke` or remove GoogleMCP at
<https://myaccount.google.com/connections>. Then follow the recovery steps
in [docs/manual-steps.md](docs/manual-steps.md#recovery). If the secret was
committed, treat pushed history as public even after you rewrite it.

The threat model and the controls behind it are in
[docs/security.md](docs/security.md).
