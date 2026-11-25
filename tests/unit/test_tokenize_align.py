from __future__ import annotations

from calder.infer.align import field_spans, identity, nw_align, progressive_align
from calder.infer.pipeline import _opcode_position
from calder.infer.streams import Frame
from calder.infer.tokenize import (
    IEEE488_COMMON,
    generic_tokens,
    is_scpi_like,
    loose_signature,
    scpi_tokens,
    shape_signature,
)
from calder.runtime.protocol import normalize_header, split_args
from calder.session.models import Direction


def test_scpi_tokens():
    t = scpi_tokens("SOUR:TEMP 37.50")
    assert t is not None
    assert (t.header, t.args, t.key, t.is_query) == ("SOUR:TEMP", ("37.50",), "SOUR:TEMP", False)
    q = scpi_tokens(":meas:temp?")
    assert q is not None
    assert q.is_query
    assert q.key == "MEAS:TEMP?"
    c = scpi_tokens("*IDN?")
    assert c is not None
    assert c.header in IEEE488_COMMON


def test_scpi_arguments_respect_quotes():
    t = scpi_tokens('DISP:TEXT "a,b",2')
    assert t is not None
    assert t.args == ('"a,b"', "2")
    assert split_args('0,"No error"', ",") == ["0", '"No error"']


def test_scpi_detection():
    assert is_scpi_like(["*IDN?", "SOUR:TEMP 25", "OUTP ON"])[0]
    assert not is_scpi_like(["Z", "T", "Q"])[0]


def test_generic_tokens():
    t = generic_tokens("SP37.5")
    assert (t.header, t.header_sep, t.args) == ("SP", "", ("37.5",))
    u = generic_tokens("SET 1 2 3")
    assert (u.header, u.args) == ("SET", ("1", "2", "3"))
    assert normalize_header(":sour:temp") == "SOUR:TEMP"


def test_shapes():
    assert shape_signature("ST,+0012.345 g") == shape_signature("US,-0001.200 g")
    assert shape_signature("ST,+0012.345 g") != shape_signature("Z A")
    assert loose_signature("23.41") == loose_signature("100.12")


def test_needleman_wunsch():
    pairs = nw_align(b"ABCD", b"ABXCD")
    assert [a for a, _ in pairs if a is not None] == list(b"ABCD")
    assert identity(b"AAAA", b"AAAA") == 1.0


def test_progressive_alignment_finds_constant_columns():
    seqs = [b"\x02\x01\x10AB\x03", b"\x02\x01\x11ABC\x03", b"\x02\x01\x12A\x03", b"\x02\x01\x10ABCD\x03"]
    aln = progressive_align(seqs)
    rows = aln.rows()
    for seq, row in zip(seqs, rows, strict=True):
        assert bytes(v for v in row if v is not None) == seq
    flags = aln.constant_columns()
    assert flags[0] and flags[1]  # start byte and address
    assert flags[-1]  # trailer
    spans = field_spans(aln)
    assert spans[0] == (0, 1, True)


def _frames(raws: list[bytes]) -> list[Frame]:
    return [Frame(0, Direction.TX, i, r, (i,)) for i, r in enumerate(raws)]


def test_opcode_position_clusters_by_structure():
    raws = []
    for i in range(30):
        op = [0x01, 0x10, 0x20][i % 3]
        payload = {0x01: b"", 0x10: b"\x00\x00\x20\x40", 0x20: b"\x01"}[op]
        raws.append(bytes([0x02, 0x01, op, len(payload)]) + payload + bytes([i % 7, 3]))
    pos, alts = _opcode_position(_frames(raws), [2, 4])
    assert pos == 2
    assert all("offset 2" not in a.description for a in alts)
