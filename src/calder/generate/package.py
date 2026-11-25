"""Write a generated driver package to disk."""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from importlib import resources
from pathlib import Path
from typing import Any

import jinja2

from calder.generate.context import (
    AliasCtx,
    FieldCtx,
    GenContext,
    MessageCtx,
    build_context,
    camel,
    doc_block,
)
from calder.model.io import to_runtime, validate_model
from calder.model.schema import ProtocolModel

RUNTIME_MODULES = ("__init__", "encodings", "checksums", "framing", "protocol", "transport", "client", "simulator")
SILA_NS = "http://www.sila-standard.org"
SILA_SCHEMA = "https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd"


def _pystr(value: Any) -> str:
    """A double-quoted Python string literal (JSON string syntax is valid Python)."""
    return json.dumps(str(value), ensure_ascii=False)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("calder.generate", "templates"),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
        autoescape=False,  # noqa: S701 - templates produce Python and Markdown, not HTML
    )
    env.filters["tojson"] = _pystr
    return env


def _sila_py(f: FieldCtx) -> str:
    if f.enum is not None:
        return "int" if f.enum.base == "IntEnum" else "str"
    return f.py_type


def _sila_out(f: FieldCtx, expr: str) -> str:
    if f.enum is not None:
        return f"int({expr})" if f.enum.base == "IntEnum" else f"str({expr})"
    return expr


def returned_outputs(m: MessageCtx) -> list[FieldCtx]:
    """Response fields a driver method returns (status only when the caller handles it)."""
    if m.response is None or m.return_kind == "none":
        return []
    data = [o for o in m.response.outputs if o.role != "status"]
    if not m.check_status:
        return [o for o in m.response.outputs if o.role == "status"] + data
    return data


def _finalize(ctx: GenContext) -> None:
    for m in ctx.messages + ctx.responses:
        lines = m.doc.strip().split("\n")
        m.doc_first = lines[0].strip().replace('"""', "'''")  # type: ignore[attr-defined]
        m.doc_rest = doc_block("\n".join(lines[1:]).strip(), 8)  # type: ignore[attr-defined]
        for f in m.inputs + m.outputs:
            f.sila_id = camel(f.name)  # type: ignore[attr-defined]
            f.sila_py = _sila_py(f)  # type: ignore[attr-defined]
            f.sila_arg = (  # type: ignore[attr-defined]
                f"{f.enum.name}({f.sila_id})" if f.enum is not None else f.sila_id  # type: ignore[attr-defined]
            )
    for m in ctx.messages:
        data_out = returned_outputs(m)
        m.value_attr = data_out[0].name if m.return_kind == "value" else ""  # type: ignore[attr-defined]
        first = m.doc.strip().split("\n")[0]
        extra = " Actuating: requires confirm=true." if m.actuating else ""
        m.tool_description = f"{first} ({m.classification}).{extra}"  # type: ignore[attr-defined]
        if m.return_kind == "none":
            m.tool_return = "str"  # type: ignore[attr-defined]
            m.return_check = "type(None)"  # type: ignore[attr-defined]
            m.sila_return = "None"  # type: ignore[attr-defined]
        elif m.return_kind == "value":
            m.tool_return = m.return_type  # type: ignore[attr-defined]
            m.return_check = "(int, float)" if m.return_type == "float" else m.return_type  # type: ignore[attr-defined]
            m.sila_return = _sila_py(data_out[0])  # type: ignore[attr-defined]
            m.sila_result = _sila_out(data_out[0], "result")  # type: ignore[attr-defined]
        else:
            m.tool_return = "dict[str, Any]"  # type: ignore[attr-defined]
            m.return_check = m.return_type  # type: ignore[attr-defined]
            m.sila_return = "tuple[" + ", ".join(_sila_py(o) for o in data_out) + "]"  # type: ignore[attr-defined]
            m.sila_results = [_sila_out(o, f"result.{o.name}") for o in data_out]  # type: ignore[attr-defined]
    examples: dict[str, str] = {}
    for m in ctx.messages:
        for part in [p for p in m.example_args.split(", ") if "=" in p]:
            name, _, literal = part.partition("=")
            examples.setdefault(name, literal)
    for a in ctx.aliases:
        names = [p.split(":")[0].strip() for p in a.params.split(",") if p.strip()]
        a.param_names = ", ".join(f"{n}={n}" for n in names)  # type: ignore[attr-defined]
        if all(n in examples for n in names):
            a.test_args = ", ".join(f"{n}={examples[n]}" for n in names)  # type: ignore[attr-defined]
        else:
            a.test_args = None  # type: ignore[attr-defined]


def _enum_names(text: str, ctx: GenContext, *, classes: bool = False) -> list[str]:
    """Names from the generated messages module that ``text`` uses."""
    names = {e.name for e in ctx.enums}
    if classes:
        names |= {m.class_name for m in ctx.messages + ctx.responses}
    found = set(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", text))
    return sorted(found & names)


def _message_imports(ctx: GenContext) -> list[str]:
    names: set[str] = set()
    for m in ctx.messages:
        names.add(m.class_name)
        if m.response is not None and m.return_kind != "none":
            names.add(m.response.class_name)
        for f in m.inputs:
            if f.enum is not None:
                names.add(f.enum.name)
        if m.return_kind == "value" and m.response is not None:
            for o in m.response.outputs:
                if o.enum is not None and o.name == getattr(m, "value_attr", ""):
                    names.add(o.enum.name)
    for s in ctx.streams:
        names.add(s.message.class_name)
    return sorted(names)


def _sila_type(f: FieldCtx) -> ET.Element:
    dt = ET.Element(f"{{{SILA_NS}}}DataType")
    basic_name = {"float": "Real", "int": "Integer", "str": "String", "bytes": "Binary"}.get(f.py_type, "String")
    if f.enum is not None:
        basic_name = "Integer" if f.enum.base == "IntEnum" else "String"
        constrained = ET.SubElement(dt, f"{{{SILA_NS}}}Constrained")
        inner = ET.SubElement(constrained, f"{{{SILA_NS}}}DataType")
        ET.SubElement(inner, f"{{{SILA_NS}}}Basic").text = basic_name
        constraints = ET.SubElement(constrained, f"{{{SILA_NS}}}Constraints")
        values = ET.SubElement(constraints, f"{{{SILA_NS}}}Set")
        for _mem, lit, _label in f.enum.members:
            ET.SubElement(values, f"{{{SILA_NS}}}Value").text = lit.strip("'\"")
        return dt
    ET.SubElement(dt, f"{{{SILA_NS}}}Basic").text = basic_name
    return dt


def _text(parent: ET.Element, tag: str, text: str) -> None:
    ET.SubElement(parent, f"{{{SILA_NS}}}{tag}").text = text


def _display(name: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)[:255] or name


def feature_xml(ctx: GenContext) -> str:
    """SiLA 2 Feature Definition (FDL) for the generated driver's commands."""
    ET.register_namespace("", SILA_NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root = ET.Element(
        f"{{{SILA_NS}}}Feature",
        {
            "SiLA2Version": "1.0",
            "FeatureVersion": "1.0",
            "Originator": "org.calder",
            "Category": "liberated",
            "Locale": "en-us",
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation": f"{SILA_NS} {SILA_SCHEMA}",
        },
    )
    _text(root, "Identifier", ctx.feature_id)
    _text(root, "DisplayName", _display(ctx.feature_id))
    _text(
        root, "Description", f"Commands of the {ctx.title}, inferred by Calder from recorded vendor-software sessions."
    )
    for m in ctx.messages:
        cmd = ET.SubElement(root, f"{{{SILA_NS}}}Command")
        _text(cmd, "Identifier", m.sila_id)
        _text(cmd, "DisplayName", _display(m.sila_id))
        _text(cmd, "Description", m.sila_doc)
        _text(cmd, "Observable", "No")
        for f in m.inputs:
            param = ET.SubElement(cmd, f"{{{SILA_NS}}}Parameter")
            _text(param, "Identifier", f.sila_id)  # type: ignore[attr-defined]
            _text(param, "DisplayName", _display(f.sila_id))  # type: ignore[attr-defined]
            _text(param, "Description", f.doc or f.name)
            param.append(_sila_type(f))
        if m.response is not None and m.return_kind != "none":
            for o in returned_outputs(m):
                resp = ET.SubElement(cmd, f"{{{SILA_NS}}}Response")
                _text(resp, "Identifier", camel(o.name))
                _text(resp, "DisplayName", _display(camel(o.name)))
                _text(resp, "Description", o.doc or o.name)
                resp.append(_sila_type(o))
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _copy_runtime(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    pkg = resources.files("calder.runtime")
    for name in RUNTIME_MODULES:
        source = pkg.joinpath(f"{name}.py").read_text(encoding="utf-8")
        if name == "__init__":
            source = '"""Calder runtime (copied verbatim from the calder package; do not edit)."""\n'
        (target / f"{name}.py").write_text(source, encoding="utf-8")


def generate(
    model: ProtocolModel,
    out: str | Path,
    *,
    name: str | None = None,
    sila: bool = True,
    mcp: bool = True,
    force: bool = False,
) -> Path:
    """Generate the package. Returns the output directory."""
    validate_model(model)
    ctx = build_context(model, name=name)
    _finalize(ctx)
    out_dir = Path(out)
    if out_dir.exists() and any(out_dir.iterdir()):
        if not force:
            raise FileExistsError(f"{out_dir} is not empty (use --force to overwrite)")
        pkg_dir = out_dir / ctx.package
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
        tests_dir = out_dir / "tests"
        if tests_dir.exists():
            shutil.rmtree(tests_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg = out_dir / ctx.package
    pkg.mkdir()
    env = _env()
    imports = _message_imports(ctx)
    int_enums = any(e.base == "IntEnum" for e in ctx.enums)
    str_enums = any(e.base == "StrEnum" for e in ctx.enums)

    def render(template: str, **extra: Any) -> str:
        return env.get_template(template).render(ctx=ctx, **extra)

    (pkg / "protocol.json").write_text(json.dumps(to_runtime(model), indent=1) + "\n", encoding="utf-8")
    (pkg / "py.typed").write_text("", encoding="utf-8")
    _copy_runtime(pkg / "_runtime")
    (pkg / "messages.py").write_text(
        render("messages.py.j2", int_enums=int_enums, str_enums=str_enums), encoding="utf-8"
    )
    (pkg / "driver.py").write_text(render("driver.py.j2", imports=imports), encoding="utf-8")
    (pkg / "simulator.py").write_text(render("simulator.py.j2"), encoding="utf-8")
    (pkg / "__init__.py").write_text(
        f'"""Open driver for the {ctx.title}, generated by Calder."""\n\n'
        f"from .driver import {ctx.class_name}\n\n"
        f'__all__ = ["{ctx.class_name}"]\n__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    tests = out_dir / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    test_text = render("tests/test_driver.py.j2", test_imports=[])
    (tests / "test_driver.py").write_text(
        render("tests/test_driver.py.j2", test_imports=_enum_names(test_text, ctx, classes=True)), encoding="utf-8"
    )
    if mcp:
        mcp_text = render("mcp_server.py.j2", imports=[], uses_asdict=True)
        mcp_imports = _enum_names(mcp_text.split("def create_server", 1)[1], ctx)
        uses_asdict = "asdict(" in mcp_text
        (pkg / "mcp_server.py").write_text(
            render("mcp_server.py.j2", imports=mcp_imports, uses_asdict=uses_asdict), encoding="utf-8"
        )
    if sila:
        sila_dir = pkg / "sila"
        sila_dir.mkdir()
        (sila_dir / "__init__.py").write_text('"""SiLA 2 feature and server."""\n', encoding="utf-8")
        (sila_dir / f"{ctx.feature_id}.sila.xml").write_text(feature_xml(ctx), encoding="utf-8")
        sila_text = render("sila_server.py.j2", imports=[])
        sila_imports = _enum_names(sila_text.split("class ", 1)[1], ctx)
        (sila_dir / "server.py").write_text(render("sila_server.py.j2", imports=sila_imports), encoding="utf-8")
    if mcp and sila:
        (tests / "test_servers.py").write_text(render("tests/test_servers.py.j2"), encoding="utf-8")
    actuating = ", ".join(f"`{m.method}`" for m in ctx.messages if m.actuating)
    (out_dir / "README.md").write_text(render("README.md.j2", actuating_names=actuating), encoding="utf-8")
    (out_dir / "pyproject.toml").write_text(render("pyproject.toml.j2"), encoding="utf-8")
    return out_dir


__all__ = ["AliasCtx", "MessageCtx", "feature_xml", "generate"]
