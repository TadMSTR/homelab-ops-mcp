# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use one of these channels:

- **GitHub private disclosure:** Use the [Security tab](https://github.com/TadMSTR/homelab-ops-mcp/security/advisories/new) to submit a private advisory.
- **Email:** Send a description to `security.i9v75@8alias.com` with the subject line `[homelab-ops-mcp] Security Report`.

Include as much detail as possible: the affected component, steps to reproduce, and potential impact.

## Scope

This server's purpose is unauthenticated shell execution and filesystem access for
whatever can reach it. A client that can connect running commands or reading files is
the design, not a vulnerability. Its controls are the ones below, and a way around any
of them is in scope.

**In scope:**

- Reaching the server from beyond its intended boundary: a bypass of the loopback bind
  default, or of the `Host`/`Origin` check (DNS rebinding from a browser on the host)
- Path validation bypasses that make a file tool or `run_command`'s `cwd` act on a path
  other than the absolute path given
- Secrets or environment values reaching the log at `INFO` or above, or reaching
  telemetry (the child-environment logging is designed to record counts and
  caller-written names only)
- Tool arguments that exhaust the shared server process (memory, CPU, or a call that
  never returns) despite the documented bounds
- Dependency vulnerabilities with a plausible exploitation path in this server's usage

**Out of scope:**

- Anything a connected client can do through the tools as documented
- Deployments that bind to a non-loopback address on an untrusted network, or list
  untrusted names in `SYSTEM_OPS_ALLOWED_HOSTS`
- Issues that require control of the server's own environment variables or command line
  (operator-controlled trust boundaries, not input attack surfaces)
- Vulnerabilities in the host system or in the MCP SDK itself (report those upstream)

## Response Expectations

| Stage | Timeline |
|-------|----------|
| Acknowledgement | Within 3 business days |
| Initial assessment | Within 7 business days |
| Fix or remediation plan | Within 30 days for critical/high; 60 days for medium/low |

This is a personal project maintained by one developer. Response times are best-effort.
If you haven't heard back within 3 business days, a follow-up email is welcome.

## Disclosure

Coordinated disclosure is preferred. Please allow time for a fix to be released before
public disclosure. The CHANGELOG documents remediated findings at an appropriate level
of detail after each release.
