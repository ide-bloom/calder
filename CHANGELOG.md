# Changelog

All notable changes to Calder are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-25

### Added

- Capture DLL (`calder_capture32.dll`, `calder_capture64.dll`, Microsoft Detours) injected only into
  applications launched through Calder: Win32 serial and named pipes (including overlapped I/O,
  completion ports and APC completions), serial line settings, VISA (raw and formatted I/O),
  FTDI D2XX, WinUSB, HID and Winsock, delivered through a shared-memory ring.
- UI Automation recorder (`calder-uia.exe`) for invoke, value, selection and toggle events on the
  same clock as the wire events, and a UI driver for scripted demos and tests.
- Passive serial tap recording and USBPcap/Wireshark `pcapng`/`pcap` import.
- `.libsession` container (zip of `events.jsonl`, `ui.jsonl`, `meta.json`, `notes.md`).
- Inference: ASCII terminators, length-field and fixed-length binary framing, timing-gap framing,
  parametrized CRC search over the CRC RevEng catalogue plus sums, XOR/LRC and Fletcher-16,
  SCPI / IEEE 488.2 tokenization, opcode clustering, progressive multiple sequence alignment,
  encoding search, UI value correlation, enumerations, counters, echoes, status codes, reflected
  values, request/response pairing, k-tails state machines and preconditions with disablers;
  Modbus RTU, ASCII and TCP recognition; optional manual (PDF) and naming assists.
- Versioned protocol model with evidence links and a JSON schema.
- Local review UI with timeline, messages, fields, framing/checksum and state machine views and
  audited edits.
- Generator for `liberated_<name>` packages: typed driver, messages, replay simulator, tests,
  SiLA 2 feature definition and server, and an MCP server that requires confirmation for
  actuating commands.
- `verify` with read, replay and explore phases inside a safety envelope.
- Instrument simulators (SCPI thermostat, binary syringe pump, Modbus temperature controller,
  fixed-width balance, HID stirrer) and a WPF test vendor application.

[Unreleased]: https://github.com/ide-bloom/calder/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ide-bloom/calder/releases/tag/v0.1.0
