# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/ide-bloom/calder/security/advisories/new)
rather than in a public issue. Include the Calder version, what you observed, and the steps to
reproduce it. We aim to acknowledge reports within five working days.

## Scope

Calder deliberately injects a DLL into vendor applications and sends commands to laboratory
instruments, so these areas matter most:

- **Injection scope.** The capture DLL must only ever load into applications launched through
  `calder record`. It is inert without the recorder's shared-memory ring.
- **The safety envelope.** Any way to make `calder verify`, a generated driver's MCP server or
  another component send a command that was never observed, skip a confirmation, or exceed the
  limits is a security issue. See `docs/safety.md`.
- **The review UI.** It binds to `127.0.0.1`, requires a per-launch token and rejects foreign
  `Host` headers. Bypasses are in scope.
- **Parsing untrusted files.** Sessions, models, pcapng captures and manuals may come from
  others; crashes or code execution while parsing them are in scope.

## Supported versions

Security fixes are released for the latest minor version.

## Handling recordings

Sessions contain every byte the vendor software exchanged with the instrument and the values
typed into its UI. Treat them as confidential and share them only when you are allowed to.
The optional naming assist sends message keys, examples, UI labels and manual text to the
endpoint you configure; leave it off for confidential protocols. API keys are read from
`CALDER_NAMING_API_KEY` and never written to models.
