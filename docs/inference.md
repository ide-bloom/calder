# Inference

`calder.infer.pipeline.infer` turns sessions into a protocol model. Each step below records its
evidence; thresholds are listed so every heuristic is visible and tested.

## 1. Streams

`infer/streams.py` groups data events by resource and direction. Without `--stream`, the busiest
bidirectional stream is chosen. With several sessions, each contributes its own busiest stream of
the same transport kind, because recordings of one instrument often name the resource differently.
A stream is ASCII when at least 97% of its bytes are printable (or tab, CR, LF); HID and WinUSB
streams are always treated as binary messages.

## 2. Framing

**ASCII.** Candidate terminators (`\r\n`, `\n\r`, `\n`, `\r`, ETX, `;`, NUL) are scored by the
fraction of reads/writes that end with them, plus 0.25 when every stream ends on one. `\r\n` wins
over `\n` when no bare `\n` occurs.

**Binary**, in order:

1. HID/WinUSB: one frame per transfer.
2. Modbus RTU recognition (below): frames delimited by silent intervals of 3.5 character times
   (at least 2 ms).
3. Length fields: start sequences are the 1- or 2-byte prefixes of at least 90% of host writes
   (writes usually carry whole frames). For each start (and none), offset 0–7, width 1/2/4,
   endianness and adjustment −4…23 (frame length = value + adjust), the whole stream in every
   session and direction must parse exactly into frames that begin with the start sequence, with
   at least two distinct lengths. Candidates are ranked by **agreement**: the product of the
   fraction of read/write ends that fall on frame boundaries and the fraction of frame boundaries
   that fall on read/write ends. Ties prefer a one-byte start, then narrow fields and early
   offsets. A one-byte length preceded by an always-zero byte is read as a two-byte big-endian
   field. When a byte is constant at offset 0 of every frame, it becomes the start byte.
4. Fixed length: every read and write has the same size.
5. Otherwise, silent-interval framing.

## 3. Checksums

`infer/checksum.py` searches:

- all 63 catalogued CRCs of the CRC RevEng catalogue (widths 8, 16, 32), each verified against its
  published check value in the tests;
- the parametrized space of the catalogued polynomials of each width with initial value
  {0, all-ones, catalogued inits}, reflection in = out ∈ {false, true} and final XOR
  {0, all-ones};
- additive sums (8/16 bit), two's- and ones'-complement sums, XOR-8 (LRC), Fletcher-16 (the Modbus
  CRC is in the catalogue);

over coverage starting at bytes 0–3 and ending at the checksum, with the checksum at the frame end
or before a one-byte trailer, raw in either byte order or as ASCII hex. Distinct frames are tested
(up to 200), and a candidate is accepted only if it validates every one. Ranking: catalogued
CRCs, then simple sums, then parametrized CRCs; wider checks; checksums at the very end; widest
coverage. Equivalent explanations (for example an initial value equal to the CRC of a start byte)
are kept as alternatives.

## 4. Message typing

- **SCPI** when at least 90% of host messages parse as SCPI headers and some use hierarchy,
  `?` queries or IEEE 488.2 common commands. Type key: normalized header (`SOUR:TEMP?`).
  Arguments are split on commas outside quotes. Compound (`;`) lines are typed as generic text.
- **Generic ASCII**: header = leading letters and punctuation; arguments follow a space (or nothing,
  as in `SP37.5`) and are split on commas or spaces.
- **Binary**: positions before the minimum frame length (excluding framing and checksum bytes) are
  classified per direction. Constant positions before the opcode are header constants (an
  *address* when the same non-0x00/0xFF value appears in both directions). A position is a
  *counter* when consecutive frames differ by +1 (mod 256) in at least 90% of at least four steps.
  The *opcode* is the position whose values best partition frames into groups of homogeneous
  length (score = homogeneous fraction − 0.002 × groups − 0.001 × offset, at least 0.8). Clusters
  whose members still differ in length are split by length.
- **Alignment**: `infer/align.py` implements Needleman–Wunsch alignment and progressive multiple
  sequence alignment with a UPGMA guide tree and profile merging (after the Protocol Informatics
  project and Discoverer, Cui et al. 2007). Members of an opcode group that differ in length are
  aligned before the group is split by length, and the aligned constant and variable spans are
  recorded in the resulting types' evidence. Equal-length members are compared column by column
  directly (their alignment is the identity).

## 5. Responses

`infer/pairing.py` pairs each request with the instrument frames that arrive after it and before
the next request (within 2 s). Per request type, the majority shape of the first candidate is the
response shape; each request is paired with its first candidate of that shape, which skips
streamed frames. A request type has a response when at least 50% are answered (SCPI: queries do,
set commands do not unless answered in at least 80% of cases). Unpaired instrument frames are
unsolicited and clustered by fixed-width shape. Timeouts are four times the slowest observed
latency, clamped to 0.25–5 s.

## 6. Field typing

`infer/fields.py` segments each variable region by dynamic programming over spans of 1, 2, 4 and
8 bytes. Scores:

| Interpretation | Score |
|---|---|
| UI-correlated (any encoding, including scaled integers ×10^−4…10^2) | 3.0 per varying byte + 0.1 per constant byte + match rate |
| IEEE 754 single/double, all values finite, magnitude 10^−6…10^9 or 0, short decimals (≤7/≤12 significant digits) | 1.0 per byte |
| Multi-byte integer with a carry pattern (most significant byte varies, but less than the least significant; sign-extended bytes compared as signed) | 0.6 per varying byte + 0.05 per constant byte |
| Multi-byte integer otherwise | 0.35 per varying byte + 0.05 per constant byte |
| Single-byte integer | 0.5 |
| BCD (all nibbles decimal) | 0.45 per varying byte + 0.05 per constant byte |
| Constant byte | 0.2 |

Scoring varying bytes rather than width keeps zero padding out of numbers. Integers are signed
when every value with the top bit set is a small negative number. Modbus registers are always
16-bit big-endian words.

ASCII tokens are integers, decimals (fixed decimals when all agree, else free with a minimum),
exponent notation, quoted strings or character data. Character data with 2–12 distinct values is
an enumeration. A host token that never varied (at least three times) is a constant; instrument
values are never constants, because responses can change later. Fixed-width lines are split into
number, word, space and punctuation tokens; numbers keep their sign, padding and decimals.

## 7. UI correlation

`infer/correlate.py`:

- **Values.** For each command, the latest value of each UI element up to 30 s before it (and up to
  0.5 s after, because UI Automation events arrive late) is compared with each candidate
  interpretation; responses are compared with values shown within 3 s after them. A field
  correlates when it matches in at least 90% of instances with a UI value, at least two, and on at
  least two distinct values. The field takes the UI label as its name, units parsed from
  `Name (unit)`, `Name [unit]` or `Name in unit`, and its observed range.
- **Enumerations.** Field values that map one-to-one onto selection or toggle values (90%
  consistent, at least two values) become an enum labelled with the UI choices.
- **Bindings.** A button is bound to a message type when the type follows at least 80% of its
  clicks (at least two) within 0.5 s (from 0.5 s before the click), with a lift of at least 3 over
  the type's probability in a window at a random time. Field values constant across bound
  instances become the binding's constants (e.g. Start → `OUTP ON`).

## 8. Patterns

Echoes (a response field equals the request's field at the same offset in at least 95% of
exchanges, and the request field varies), status codes (the first payload byte of at least two
response types, with one value in at least 70% of frames and at most 8 values; that value is
success; SCPI `SYST:ERR?` codes, where 0 is success), and reflected values (a response field
equals the last value set by a command in at least 90% of at least two cases on at least two
values, or SCPI `X?` reporting `X`).

## 9. Classification

`infer/classify.py` classifies commands from their names, headers, IEEE 488.2 descriptions and
bound UI controls. Words such as start, run, move, home, heat, cool, dispense, infuse, withdraw,
stir, open, close, output, trigger, stop and abort make a command *actuating*; any actuating cue
wins. Queries without parameters are *read-only*. Other commands are *configuring*. Instrument
messages are read-only. Users confirm classifications in the review UI.

## 10. State machine and preconditions

`infer/statemachine.py` builds a prefix tree of each session's command sequence and merges states
with equal k-tails (Biermann & Feldman 1972; default k = 2).

P is a precondition of M when every occurrence of M (at least two) is preceded by P in its
session, and only state-changing commands can be P (queries cannot enable anything). D *disables*
P when it was seen between P and a later re-sent P that was followed by M, and M never occurred
between D and the next P; with disablers, P must also follow the last D before each M. A
requirement that immediately precedes M in at least 90% of occurrences is the vendor batching one
user action ("setpoint" then "rate" on Apply) and is dropped.

## 11. Modbus

`infer/modbus.py` recognizes RTU (CRC-16/MODBUS little-endian, valid function codes and lengths,
one unit address, in at least 95% of frames), ASCII (`:` hex lines with a valid LRC) and TCP (MBAP
protocol id 0 with a consistent length). Types are keyed by function and register
(`06:0000`, `03:0000x3`).

## 12. Manual and naming assists

With `--manual`, `infer/manual.py` (pdfplumber) extracts lines and table rows that start with SCPI
mnemonics, IEEE 488.2 commands or hex opcodes, reduces SCPI long forms to short forms
(`SOURce:TEMPerature` → `SOUR:TEMP`) and attaches matching descriptions to messages.

With `--naming-endpoint` (opt-in), `infer/naming.py` sends keys, examples, UI labels and manual text
to an OpenAI-compatible chat endpoint and stores valid identifiers as `suggested_name` /
`suggested_doc`. Suggestions are never applied automatically, and Calder works fully offline
without them.

## 13. Self-check

After the model is built, every observed frame is decoded with the runtime codec built from the
model, and every request is re-encoded and compared byte for byte (except frames with counters).
Disagreements become warnings and lower the message's confidence.
