# Generation

`calder generate model.json --out DIR [--name NAME] [--sila/--no-sila] [--mcp/--no-mcp] [--force]`
writes a Python project:

```
DIR/
  pyproject.toml          ruff (E501 ignored) and mypy --strict configuration, scripts, extras
  README.md               commands, classifications, preconditions, warnings
  liberated_NAME/
    __init__.py
    protocol.json         the runtime projection of the model (rejected elements removed)
    _runtime/             Calder's standard-library runtime, copied verbatim
    messages.py           enums and typed request/response dataclasses
    driver.py             NAMEDriver: one typed method per command, UI shortcuts, stream readers
    simulator.py          replay simulator (python -m liberated_NAME.simulator --port 5025)
    mcp_server.py         MCP server (stdio)
    sila/
      NAMEInstrument.sila.xml   SiLA 2 feature definition
      server.py                 SiLA 2 server (sila2 package)
  tests/
    test_driver.py        codec, round-trip, simulator and per-method tests
    test_servers.py       MCP and SiLA 2 server tests
```

## Driver

- **Transport.** `NAMEDriver.connect(resource)` opens the captured transport with the recorded line
  settings: pyserial (serial), pyvisa (VISA), TCP sockets, FTDI D2XX through ctypes, pyusb
  (WinUSB) or hidapi (HID). `tcp://host:port` works for every transport and reaches the simulator
  or a network bridge; HID/WinUSB reports use a 2-byte length prefix over TCP.
  `NAMEDriver.simulated()` connects in-process to the replay simulator.
- **Framing codec with checksum.** Frames are built and parsed by `_runtime/protocol.py` from
  `protocol.json`: terminators, start bytes, length fields, counters and checksums are filled in
  automatically.
- **Typed methods.** Parameters use engineering units (scaling happens in the codec) and enums for
  enumerations. Docstrings cite the wire format, classification, evidence (frames, method,
  confidence, "uncertain" below 0.8), UI controls, preconditions and manual text. Queries return
  the response value, or a dataclass when the response has several fields; a status field is
  returned (instead of raising) when the caller receives it.
- **Preconditions.** The client raises `PreconditionError` when a command is sent before its
  inferred requirements (disabled again by disabling commands). Pass
  `check_preconditions=False` to override.
- **Errors.** `ResponseTimeoutError`, `UnexpectedResponseError`, `InstrumentStatusError` (a status
  code never observed as success).
- **UI shortcuts.** Each vendor-UI button becomes a method that sends what the button sent, with
  the recorded constants (for example `start()` → `outp(OutpValue.ON)`).
- **Streams.** Unsolicited messages are read with `read_<message>(timeout)`.

## Replay simulator

Answers byte-identical recorded requests with the recorded response; otherwise it uses the
response template of the request's type and updates echoed fields and values that reflect earlier
commands. Streamed messages are replayed periodically.

## MCP server

Built with the official MCP Python SDK (`MCPServer`). Every command is a tool with
`read_only_hint`/`destructive_hint` annotations. Actuating commands (and UI shortcuts that send
one) take `confirm: bool = False` and raise a tool error unless `confirm=true`. Read-only and
configuring tools run directly.

## SiLA 2

The feature definition has one unobservable command per host command: parameters are `Real`,
`Integer`, `String` or `Binary`, enums are constrained by a `Set`, and query results are responses.
The server implements the feature with `sila2.server.SilaServer` and forwards each call to the
driver.

## Tests

The generated tests check that every recorded exchange decodes, that requests re-encode byte for
byte, that the simulator answers recorded requests, that each command method works against the
simulator, that preconditions are enforced, that UI shortcuts run, that the MCP server lists all
tools and refuses an actuating call without confirmation, and that the SiLA 2 server starts and
answers a client call. Calder's own test suite generates a package for every simulator and runs
`ruff check`, `mypy --strict` and `pytest` on it.
