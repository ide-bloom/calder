# Verification and the safety envelope

Physical instruments can heat, move, dispense, pressurize or apply high voltage. `calder verify`
checks a generated driver against the live instrument under these rules, implemented in
`src/calder/verify/engine.py` and tested in `tests/test_safety.py`.

```
calder verify DRIVER_DIR --resource RESOURCE [--phase read|replay|explore]
              [--allow-actuation] [--limits FILE] [--rate HZ] [--log FILE] [--session ID]
```

## Always

- A prominent warning: the operator must be present at the instrument with its own emergency stop
  within reach.
- **Rate limit**: at most `--rate` commands per second (default 2, capped at 20).
- **Immediate abort** on a timeout, a response of the wrong type or with unexpected content, a
  status code never observed as success, or a response that fails to echo the request. When the
  model has an SCPI-style error query (`...ERR?` with a status field), it is sent after every
  configuring or actuating command and a non-zero code aborts.
- **Every byte is logged**: sent and received bytes go to a JSON Lines log with timestamps
  (`verify-<time>.jsonl` by default).
- The whole plan is checked before anything is sent.

## Never

- A message type (opcode, mnemonic or Modbus function/register) that was not observed. Frames
  that do not decode as an observed, non-rejected host message are refused.

## Phases

| Phase | Sends | Requires |
|---|---|---|
| `read` (default) | Byte-identical recorded requests of read-only messages without preconditions | nothing |
| `replay` | One recorded session's requests, byte for byte, in order (`--session`, default the longest) | `--allow-actuation` and an interactive confirmation for each class present (configuring, actuating) |
| `explore` | For each command named in the limits file: its recorded preconditions, then the command with numeric parameters at the minimum, midpoint and maximum of the allowed range and enums over observed values | `--allow-actuation`, `--limits`, and confirmation per class |

Responses are compared structurally: the response type, constant fields, checksums, enum values
and echoes must match; values may differ.

## Limits file

TOML or JSON:

```toml
max_commands = 50      # optional, default 100
rate_hz = 1            # optional, overrides --rate

[commands.sour_temp]   # command names as in the driver
setpoint = { min = 20.0, max = 40.0 }

[commands.set_rate]
rate = { min = 0.5, max = 5.0, beyond_observed = false }
```

The allowed range is the intersection of the limits and the range observed in the recordings.
A field may opt out of the observed range with `beyond_observed = true`. Commands not listed are
not explored, and fields without limits are left at their recorded values.

## Elsewhere

- Generated MCP servers refuse actuating commands without `confirm=true`.
- Generated drivers enforce inferred preconditions by default.
- Rejected messages, preconditions and fields never reach generated packages or `verify`.

The envelope reduces risk; it cannot know what an instrument does physically. Stay at the
instrument.
