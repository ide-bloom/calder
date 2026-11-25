# The protocol model (schema 1.0)

`calder infer` writes a `model.json` with `"kind": "calder.protocol"` and
`"schema_version": "1.0"`. The pydantic models in `src/calder/model/schema.py` are normative;
[`protocol-model.schema.json`](protocol-model.schema.json) is the generated JSON Schema (the
test suite checks that it is current).

## Evidence

Every inferred element has an `evidence` object:

| Field | Meaning |
|---|---|
| `method` | The algorithm that produced it, e.g. `framing.length_field`, `checksum.search`, `fields.encoding_search`, `statemachine.precondition` |
| `confidence` | 0 to 1; below 0.8 the element is shown as uncertain |
| `support`, `contradictions` | Observations for and against |
| `events` | References `{session, index, ui}` into sessions |
| `alternatives` | Other explanations considered, with scores and reasons |
| `notes` | Human-readable justification |

## Top level

| Field | Content |
|---|---|
| `name`, `created`, `calder_version` | Identity |
| `family` | `generic`, `scpi`, `modbus_rtu`, `modbus_ascii` or `modbus_tcp` |
| `stream` | The analyzed resource |
| `sessions` | `{id, path, sha256, events, ui_events, source}` per input session |
| `transport` | Kind, resource, serial line settings, transport settings (VID/PID, endpoints) |
| `framing` | Kind (`delimited`, `length_prefixed`, `fixed_length`, `message`, `modbus_rtu`), encoding, terminators, start sequence, length field (`offset`, `width`, `endian`, `adjust`: frame length = value + adjust), fixed length, silent interval |
| `checksum` | Algorithm name, width, CRC parameters, coverage (`start`, `end` relative to the frame end), `position`, `endian`, `encoding` (`raw` or `ascii_hex`), `covered` (`hex` for hex-decoded coverage) |
| `messages` | Message types (below) |
| `state_machine` | k-tails automaton: `k`, states, transitions with counts |
| `exchanges` | Recorded requests with their responses and latencies (used by the simulator and `verify`) |
| `unsolicited` | Recorded streamed frames (hex) |
| `warnings` | Inference warnings, including self-check disagreements |
| `manual` | Manual file, hash, pages, commands found |
| `audit` | Review edits: time, actor, action, target, before, after |

## Message types

`id` (stable), `name` (editable identifier), `direction` (`tx`/`rx`), `kind` (`command`, `query`,
`response`, `unsolicited`), `key` (SCPI header, opcode hex or Modbus function/register),
`layout` (`tokens`, `columns`, `binary`), `header`, `header_sep`, `arg_sep`, `length`, `fields`,
`response`, `response_timeout_ms`, `classification` (`read_only`, `configuring`, `actuating`) with
`classification_reason` and `classification_confirmed`, `ui_bindings`, `preconditions`,
`examples` (raw frames with decoded values), `count`, `status` (`inferred`, `accepted`, `rejected`,
`edited`), `suggested_name`/`suggested_doc` (naming assist, never applied automatically), `doc`,
`manual`.

## Fields

`id`, `name`, `role` (`value`, `enum`, `counter`, `constant`, `address`, `status`, `echo`, `opcode`,
`length`, `checksum`, `start`), location (`offset`/`length` in bytes or characters, or `token`),
`encoding` (`kind` among `uint`, `int`, `float`, `bcd`, `bits`, `bytes`, `ascii_int`, `ascii_float`,
`ascii_hex`, `ascii_enum`, `ascii_string`; `width`, `endian`, `scale_exp` where
physical = raw × 10^scale_exp, `decimals`, `min_decimals`, `plus_sign`, `zero_pad`, `exponent`),
`constant`, `enum` (`wire`, `label`, `ui_value`, `count`), `units`, `label`, `ui_element`, `range`
(observed), `echo_of`, `reflects` (`[message id, field id]` of the command whose value a response
reports), `status_ok`, `status`, suggestions, `doc`.

## Preconditions

On a message: `message` (the id it requires first), `disabled_by` (ids that undo it), `status`,
`evidence`. They are hypotheses from positive traces; review them.

## Validation

`calder.model.validate_model` checks unique ids and names, that responses, preconditions,
`reflects` and exchanges reference existing elements, that value, enum and status fields have
encodings, and that binary fields have locations.
