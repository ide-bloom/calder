"""Calder command-line interface."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from calder import __version__

app = typer.Typer(
    name="calder",
    help="Watch vendor software talk to a lab instrument, infer the protocol, generate an open driver.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(stderr=True)


def _fail(message: str, code: int = 1) -> typer.Exit:
    console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"calder {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """Calder frees lab instruments from the software that locked them in."""


def _default_session_path(stem: str) -> Path:
    return Path("sessions") / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.libsession"


@app.command()
def record(
    app_path: Annotated[Path, typer.Option("--app", help="Vendor executable to launch.", exists=True, dir_okay=False)],
    transport: Annotated[str, typer.Option(help="auto, or one or more of serial,visa,ftdi,winusb,hid,tcp.")] = "auto",
    filter_spec: Annotated[
        str, typer.Option("--filter", help="Resource patterns, e.g. 'serial:COM5,tcp:*:5025'.")
    ] = "",
    ui: Annotated[bool, typer.Option("--ui/--no-ui", help="Also record UI Automation events.")] = False,
    out: Annotated[Path | None, typer.Option(help="Session file to write.")] = None,
    args: Annotated[str, typer.Option(help="Arguments for the vendor application.")] = "",
    duration: Annotated[float | None, typer.Option(help="Stop after this many seconds.")] = None,
    drive: Annotated[
        Path | None, typer.Option(help="UI script to replay against the app (reproducible demos and tests).")
    ] = None,
) -> None:
    """Launch the vendor app with the capture DLL and record a session."""
    import shlex

    from calder.capture.filters import CaptureFilter, FilterError
    from calder.capture.launcher import LaunchError
    from calder.capture.recorder import RecordOptions
    from calder.capture.recorder import record as do_record
    from calder.session.io import save_session

    try:
        filt = CaptureFilter.parse(filter_spec, transport)
    except FilterError as exc:
        raise _fail(str(exc)) from exc
    options = RecordOptions(
        app=str(app_path),
        args=shlex.split(args, posix=False),
        filter=filt,
        ui=ui,
        drive_script=str(drive) if drive else None,
        duration=duration,
    )
    console.print(f"Recording [bold]{app_path.name}[/] ({filt.describe()}); close the app or press Ctrl+C to stop.")
    try:
        session = do_record(options, on_status=lambda m: console.print(f"  {m}"))
    except LaunchError as exc:
        raise _fail(str(exc)) from exc
    target = out or _default_session_path(app_path.stem)
    save_session(session, target)
    wire = len(session.data_events())
    console.print(
        f"Saved {target}: {wire} data events, {len(session.ui_events)} UI events, "
        f"{session.meta.dropped_events} dropped."
    )


@app.command()
def tap(
    a: Annotated[str, typer.Option("--a", help="Adapter on the host's TX line (host to instrument).")],
    b: Annotated[str, typer.Option("--b", help="Adapter on the instrument's TX line.")],
    baud: Annotated[int, typer.Option(help="Baud rate of the tapped line.")] = 9600,
    bytesize: Annotated[int, typer.Option()] = 8,
    parity: Annotated[str, typer.Option()] = "N",
    stopbits: Annotated[float, typer.Option()] = 1.0,
    duration: Annotated[float | None, typer.Option()] = None,
    out: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Record from a passive serial tap (two receive-only adapters on a Y-cable)."""
    from calder.capture.tap import TapOptions
    from calder.capture.tap import tap as do_tap
    from calder.session.io import save_session

    console.print(f"Tapping {a} (host) and {b} (instrument) at {baud} baud; Ctrl+C to stop.")
    session = do_tap(TapOptions(a, b, baud, bytesize, parity.upper(), stopbits, duration))
    target = out or _default_session_path("tap")
    save_session(session, target)
    console.print(f"Saved {target}: {len(session.data_events())} data events.")


@app.command("import")
def import_(
    capture: Annotated[Path, typer.Argument(help="pcapng or pcap file (USBPcap or Ethernet).", exists=True)],
    out: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Convert a USB or Ethernet capture into a session."""
    from calder.capture.pcapng import CaptureFormatError, import_capture
    from calder.session.io import save_session

    try:
        session = import_capture(capture)
    except CaptureFormatError as exc:
        raise _fail(str(exc)) from exc
    target = out or capture.with_suffix(".libsession")
    save_session(session, target)
    console.print(f"Saved {target}: streams {', '.join(session.streams())}")


@app.command()
def infer(
    sessions: Annotated[list[Path], typer.Argument(help="One or more .libsession files.", exists=True)],
    out: Annotated[Path, typer.Option(help="Model file to write.")] = Path("model.json"),
    name: Annotated[str | None, typer.Option(help="Protocol name.")] = None,
    stream: Annotated[str | None, typer.Option(help="Resource to analyze (default: busiest).")] = None,
    manual: Annotated[Path | None, typer.Option(help="Instrument manual (PDF) for command tables.")] = None,
    k: Annotated[int, typer.Option(help="k for k-tails state merging.")] = 2,
    naming_endpoint: Annotated[
        str | None, typer.Option(help="Opt-in: OpenAI-compatible endpoint for name suggestions.")
    ] = None,
    naming_model: Annotated[str, typer.Option(help="Model name for --naming-endpoint.")] = "default",
) -> None:
    """Build a protocol model from one or more sessions."""
    from calder.infer.pipeline import InferenceError, InferOptions
    from calder.infer.pipeline import infer as do_infer
    from calder.model.io import save_model
    from calder.session.io import load_session, session_digest

    loaded = [load_session(p) for p in sessions]
    base = out.resolve().parent
    rel = [os.path.relpath(p.resolve(), base) for p in sessions]
    try:
        model = do_infer(
            loaded,
            InferOptions(
                name=name,
                stream=stream,
                k=k,
                manual=str(manual) if manual else None,
                session_paths=rel,
                session_digests=[session_digest(p) for p in sessions],
            ),
        )
    except (InferenceError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    if naming_endpoint:
        from calder.infer.naming import suggest

        applied = suggest(
            model, endpoint=naming_endpoint, model_name=naming_model, api_key=os.environ.get("CALDER_NAMING_API_KEY")
        )
        console.print(f"Naming assist: {applied} suggestions (marked as suggested, not applied).")
    save_model(model, out)
    table = Table(title=f"{model.name}: {model.family.value}, {model.framing.kind.value} framing", show_lines=False)
    for col in ("message", "kind", "class", "frames", "confidence"):
        table.add_column(col)
    for m in model.messages:
        table.add_row(
            m.name,
            m.kind.value,
            m.classification.value if m.direction.value == "tx" else "",
            str(m.count),
            f"{m.evidence.confidence:.2f}",
        )
    console.print(table)
    if model.checksum:
        console.print(f"Checksum: {model.checksum.algorithm}")
    for w in model.warnings:
        console.print(f"[yellow]warning:[/] {w}")
    console.print(f"Saved {out}")


@app.command()
def review(
    model: Annotated[Path, typer.Argument(help="Model file.", exists=True)],
    port: Annotated[int, typer.Option(help="Port (0 = random).")] = 0,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open a browser.")] = False,
) -> None:
    """Open the local review UI (127.0.0.1, per-launch token)."""
    from calder.web.app import serve

    serve(model, port=port, open_browser=not no_browser)


@app.command()
def generate(
    model: Annotated[Path, typer.Argument(help="Model file.", exists=True)],
    out: Annotated[Path, typer.Option(help="Output directory.")],
    name: Annotated[str | None, typer.Option(help="Package name suffix: liberated_<name>.")] = None,
    sila: Annotated[bool, typer.Option("--sila/--no-sila", help="Generate the SiLA 2 feature and server.")] = True,
    mcp: Annotated[bool, typer.Option("--mcp/--no-mcp", help="Generate the MCP server.")] = True,
    force: Annotated[bool, typer.Option(help="Overwrite generated files in a non-empty directory.")] = False,
) -> None:
    """Generate the driver package, simulator, tests and servers."""
    from calder.generate import generate as do_generate
    from calder.model.io import load_model

    try:
        target = do_generate(load_model(model), out, name=name, sila=sila, mcp=mcp, force=force)
    except FileExistsError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"Generated {target}. Run `pytest` there to test the driver against its simulator.")


def _interactive_confirm(cls: str, names: list[str]) -> bool:
    console.print(
        Panel(
            f"{len(names)} {cls} command type(s) will be sent: {', '.join(names)}",
            title=f"Confirm {cls} commands",
            border_style="yellow",
        )
    )
    return typer.confirm(f"Send {cls} commands to the instrument?", default=False)


@app.command()
def verify(
    driver_dir: Annotated[Path, typer.Argument(help="Generated driver directory.", exists=True)],
    resource: Annotated[str, typer.Option(help="Port, VISA resource, or host:port / tcp://host:port.")],
    phase: Annotated[str, typer.Option(help="read, replay or explore.")] = "read",
    allow_actuation: Annotated[bool, typer.Option("--allow-actuation", help="Permit replay/explore.")] = False,
    limits: Annotated[Path | None, typer.Option(help="Limits file (TOML or JSON) for explore.")] = None,
    rate: Annotated[float, typer.Option(help="Maximum commands per second (capped at 20).")] = 2.0,
    log: Annotated[Path | None, typer.Option(help="JSONL byte log (default: verify-<time>.jsonl).")] = None,
    session: Annotated[str | None, typer.Option(help="Recorded session to replay.")] = None,
) -> None:
    """Verify against the live instrument inside the safety envelope."""
    from calder.runtime.protocol import ProtocolSpec
    from calder.runtime.transport import TransportError, open_transport
    from calder.verify.engine import Limits, Phase, SafetyError, Verifier, find_protocol

    try:
        chosen = Phase(phase)
    except ValueError as exc:
        raise _fail("phase must be read, replay or explore") from exc
    spec = ProtocolSpec.load(find_protocol(driver_dir))
    res = resource
    if (
        ":" in res
        and not res.startswith(("tcp://", "socket://"))
        and res.rsplit(":", 1)[-1].isdigit()
        and spec.transport.get("kind") in ("tcp", "serial")
        and "::" not in res
    ):
        res = f"tcp://{res}"
    log_path = log or Path(f"verify-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        lim = Limits.load(limits) if limits else None
        transport = open_transport(spec.transport, res)
    except (SafetyError, TransportError, OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    verifier = Verifier(
        spec,
        transport,
        phase=chosen,
        allow_actuation=allow_actuation,
        limits=lim,
        confirm=_interactive_confirm,
        warn=lambda text: console.print(Panel(text, title="SAFETY", border_style="bold red")),
        rate_hz=rate,
        log_path=log_path,
        session=session,
    )
    try:
        result = verifier.run()
    except SafetyError as exc:
        raise _fail(f"refused: {exc}", 2) from exc
    finally:
        transport.close()
    console.print(f"{result.passed}/{result.sent} exchanges matched; byte log {log_path}")
    if result.aborted:
        raise _fail(f"aborted: {result.reason}", 3)


@app.command()
def simulate(
    model: Annotated[Path, typer.Argument(help="Model file or generated driver directory.", exists=True)],
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="TCP port (0 = random).")] = 5025,
) -> None:
    """Run the replay simulator as a virtual instrument on TCP."""
    from calder.model.io import load_model, to_runtime
    from calder.runtime.protocol import ProtocolSpec
    from calder.runtime.simulator import ReplaySimulator, SimulatorServer
    from calder.verify.engine import find_protocol

    if model.is_dir():
        spec = ProtocolSpec.load(find_protocol(model))
    else:
        spec = ProtocolSpec.from_dict(to_runtime(load_model(model)))
    message_mode = spec.transport.get("kind") in ("hid", "winusb")
    server = SimulatorServer(ReplaySimulator(spec), host, port, message_mode=message_mode).start()
    console.print(f"Simulating {spec.name} on tcp://{server.address} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.stop()


@app.command()
def doctor() -> None:
    """Check Python, native binaries, VISA runtime and permissions."""
    from calder.capture.launcher import LaunchError, find_native

    table = Table(title="calder doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    ok_all = True

    def row(check: str, ok: bool, detail: str, *, required: bool = True) -> None:
        nonlocal ok_all
        if required and not ok:
            ok_all = False
        table.add_row(
            check, "[green]ok[/]" if ok else ("[red]missing[/]" if required else "[yellow]optional[/]"), detail
        )

    row("Python", sys.version_info >= (3, 12), platform.python_version())
    row("platform", True, f"{platform.system()} {platform.release()} {platform.machine()}")
    windows = sys.platform == "win32"
    for name in (
        "calder_capture32.dll",
        "calder_capture64.dll",
        "calder_launch32.exe",
        "calder_launch64.exe",
        "calder-uia.exe",
    ):
        try:
            row(name, True, str(find_native(name)), required=windows)
        except LaunchError:
            row(name, False, "build native/ or install the Windows wheel", required=windows)
    try:
        import pyvisa

        try:
            rm = pyvisa.ResourceManager()
            row("VISA runtime", True, str(rm.visalib), required=False)
            rm.close()
        except (OSError, ValueError) as exc:
            row("VISA runtime", False, f"pyvisa installed, no VISA library: {exc}", required=False)
    except ImportError:
        row("VISA runtime", False, "pyvisa not installed", required=False)
    dotnet = shutil.which("dotnet")
    row(
        ".NET runtime (UI recorder)",
        dotnet is not None,
        dotnet or "install the .NET 10 Desktop Runtime",
        required=False,
    )
    try:
        import serial.tools.list_ports

        ports = [p.device for p in serial.tools.list_ports.comports()]
        row("serial ports", True, ", ".join(ports) or "none found", required=False)
    except ImportError:
        row("serial ports", False, "pyserial missing")
    writable = os.access(Path.cwd(), os.W_OK)
    row("write access (cwd)", writable, str(Path.cwd()))
    console.print(table)
    if not ok_all:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
