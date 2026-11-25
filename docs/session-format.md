# The `.libsession` format (version 1.0)

A session is a zip archive with four members. Each is plain text, so a session can be
inspected without Calder.

| Member | Content |
|---|---|
| `meta.json` | Session metadata (one JSON object) |
| `events.jsonl` | Wire events, one JSON object per line, in time order |
| `ui.jsonl` | UI events, one JSON object per line, in time order |
| `notes.md` | Free-form notes |

Readers must reject sessions whose major `format_version` differs from their own.
The pydantic models in `src/calder/session/models.py` are the normative definition.

## `meta.json`

| Field | Meaning |
|---|---|
| `format_version` | `"1.0"` |
| `session_id` | Unique id (hex) |
| `created` | ISO 8601 timestamp (UTC) |
| `calder_version` | Version that recorded the session |
| `source` | `hook`, `tap`, `pcapng` or `synthetic` |
| `app` | `{path, args, bitness}` of the recorded vendor application |
| `transports` | Transports that occur in `events.jsonl` |
| `filter` | Human-readable filter description |
| `clock` | `"qpc"`: QueryPerformanceCounter, shared by wire and UI events |
| `dropped_events` | Records the DLL dropped because the ring was full (0 in a healthy capture) |
| `extra` | Additional data, e.g. `dll`: the capture DLL's info events (attached hooks, bitness) |

## Wire events (`events.jsonl`)

| Field | Meaning |
|---|---|
| `index` | Position in the file |
| `t_ns` | Nanoseconds since the session start |
| `transport` | `serial`, `visa`, `ftdi`, `winusb`, `hid` or `tcp` (named pipes opened with `CreateFile` are `serial`) |
| `stream` | Resource identity, e.g. `serial:COM5`, `serial:\\.\pipe\name`, `tcp:10.0.0.5:5025`, `visa:USB0::...`, `ftdi:FT1234`, `hid:\\?\hid#vid_...` |
| `direction` | `tx` (host to instrument), `rx` (instrument to host) or `none` |
| `kind` | `open`, `close`, `data`, `config`, `signal`, `error` or `info` |
| `data` | Payload bytes as lower-case hex |
| `connection` | Incremented each time the resource is reopened |
| `pid`, `tid` | Process and thread that issued the call |
| `meta` | Event details, see below |

`meta` by kind:

- `open`: `path`, `remote`, `resource`, `serial_number`, `description`, `vid`, `pid`, `device_kind` (`pipe`).
- `config`: decoded line settings, e.g. `baudrate`, `bytesize`, `parity` (`N`/`E`/`O`/`M`/`S`),
  `stopbits`, `rtscts`, `dsrdtr`, `xonxoff`, `read_timeout_ms`, `write_timeout_ms`, plus `api`.
- `signal`: `api` and `function` (e.g. `SETDTR`) or `mask` (WaitCommEvent); for VISA formatted reads,
  `format` and `bytes_unavailable: true`.
- `error`: failed device calls, with `api` and `status` (Win32 error or driver status).
- `data`: optional `api` (e.g. `viPrintf`), `format`, `pipe`, `endpoint`.

## UI events (`ui.jsonl`)

| Field | Meaning |
|---|---|
| `index`, `t_ns` | As above, same clock |
| `kind` | `invoke`, `value`, `selection`, `toggle`, `focus` or `window` |
| `element` | `{name, automation_id, control_type, label, window}`; `label` is the `LabeledBy` text or, for inputs, the nearest preceding text |
| `value` | New value (text, selected item, toggle state) or null |

## Producing sessions

- `calder record`: capture DLL plus optional UI recording.
- `calder tap`: a passive serial tap.
- `calder import`: pcapng/pcap (Ethernet, raw IP, loopback, Linux cooked, USBPcap).
- `calder.session.SessionBuilder`: programmatic sessions (used by the simulators' generators).
