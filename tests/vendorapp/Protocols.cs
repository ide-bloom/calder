using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Text;

namespace Calder.VendorApp;

/// <summary>
/// The instrument connection. Pipes are opened without overlapped I/O and sockets are used
/// synchronously, the way many vendor applications are written.
/// </summary>
public sealed class Link : IDisposable
{
    private readonly Stream _stream;
    private readonly IDisposable? _owner;
    private readonly object _lock = new();

    private Link(Stream stream, IDisposable? owner)
    {
        _stream = stream;
        _owner = owner;
        _stream.ReadTimeout = 2000;
    }

    public static Link OpenPipe(string name)
    {
        var pipe = new NamedPipeClientStream(".", name, PipeDirection.InOut, PipeOptions.None);
        pipe.Connect(5000);
        return new Link(new TimeoutStream(pipe), pipe);
    }

    public static Link OpenTcp(string endpoint)
    {
        var parts = endpoint.Split(':');
        var client = new TcpClient { NoDelay = true };
        client.Connect(parts[0], int.Parse(parts[1], CultureInfo.InvariantCulture));
        return new Link(client.GetStream(), client);
    }

    public T Exchange<T>(Func<Stream, T> body)
    {
        lock (_lock)
        {
            return body(_stream);
        }
    }

    public void Dispose()
    {
        _stream.Dispose();
        _owner?.Dispose();
    }
}

/// <summary>Pipe reads in .NET ignore ReadTimeout; poll for data so a silent instrument cannot hang the app.</summary>
internal sealed class TimeoutStream(NamedPipeClientStream inner) : Stream
{
    public override int ReadTimeout { get; set; } = 2000;
    public override bool CanTimeout => true;
    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() => inner.Flush();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => inner.Write(buffer, offset, count);

    public override int Read(byte[] buffer, int offset, int count)
    {
        var deadline = Environment.TickCount64 + ReadTimeout;
        while (true)
        {
            if (PeekAvailable() > 0)
            {
                return inner.Read(buffer, offset, count);
            }
            if (Environment.TickCount64 > deadline)
            {
                throw new TimeoutException("instrument did not answer");
            }
            Thread.Sleep(2);
        }
    }

    private int PeekAvailable()
    {
        if (!NativeMethods.PeekNamedPipe(inner.SafePipeHandle, IntPtr.Zero, 0, IntPtr.Zero, out var available, IntPtr.Zero))
        {
            throw new IOException("pipe closed");
        }
        return (int)available;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) inner.Dispose();
        base.Dispose(disposing);
    }
}

internal static partial class NativeMethods
{
    [System.Runtime.InteropServices.LibraryImport("kernel32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    internal static partial bool PeekNamedPipe(Microsoft.Win32.SafeHandles.SafePipeHandle pipe, IntPtr buffer, uint size,
        IntPtr read, out uint available, IntPtr left);
}

public interface IInstrumentProtocol
{
    string SetpointLabel { get; }
    string RateLabel { get; }
    string DisplayLabel { get; }
    IReadOnlyList<string> Modes { get; }
    string? Connect(Link link);
    string? Poll(Link link);
    string? Apply(Link link, double setpoint, double rate);
    string? SetMode(Link link, int index);
    string? Start(Link link);
    string? Stop(Link link);
    void Disconnect(Link link);
}

/// <summary>SCPI thermostat: newline-terminated commands; set commands need remote mode.</summary>
public sealed class ThermostatProtocol : IInstrumentProtocol
{
    private static readonly string[] Wire = ["CONST", "RAMP", "HOLD"];
    private bool _remote;

    public string SetpointLabel => "Setpoint (°C)";
    public string RateLabel => "Rate (°C/min)";
    public string DisplayLabel => "Temperature (°C)";
    public IReadOnlyList<string> Modes { get; } = ["Constant", "Ramp", "Hold"];

    private static void Send(Stream s, string command)
    {
        var bytes = Encoding.ASCII.GetBytes(command + "\n");
        s.Write(bytes, 0, bytes.Length);
        s.Flush();
    }

    private static string ReadLine(Stream s)
    {
        var sb = new StringBuilder();
        var buf = new byte[1];
        while (true)
        {
            if (s.Read(buf, 0, 1) == 0) throw new IOException("connection closed");
            if (buf[0] == (byte)'\n') return sb.ToString();
            sb.Append((char)buf[0]);
        }
    }

    private static string Query(Link link, string command) => link.Exchange(s =>
    {
        Send(s, command);
        return ReadLine(s);
    });

    private void Command(Link link, string command)
    {
        link.Exchange(s =>
        {
            if (!_remote)
            {
                Send(s, "SYST:REM");
                _remote = true;
            }
            Send(s, command);
            return 0;
        });
    }

    private static string F(double v) => v.ToString("F2", CultureInfo.InvariantCulture);

    public string? Connect(Link link) => Query(link, "*IDN?");
    public string? Poll(Link link) => Query(link, "MEAS:TEMP?");

    public string? Apply(Link link, double setpoint, double rate)
    {
        Command(link, $"SOUR:TEMP {F(setpoint)}");
        Command(link, $"SOUR:TEMP:RATE {F(rate)}");
        return "Applied";
    }

    public string? SetMode(Link link, int index)
    {
        Command(link, $"MODE {Wire[index]}");
        return $"Mode {Modes[index]}";
    }

    public string? Start(Link link)
    {
        Command(link, "OUTP ON");
        return "Output on";
    }

    public string? Stop(Link link)
    {
        Command(link, "OUTP OFF");
        return "Output off";
    }

    public void Disconnect(Link link)
    {
        link.Exchange(s =>
        {
            Send(s, "SYST:LOC");
            return 0;
        });
        _remote = false;
    }
}

/// <summary>Binary syringe pump: STX, address, opcode, length, payload, CRC-16/IBM-3740 (big-endian).</summary>
public sealed class PumpProtocol : IInstrumentProtocol
{
    private const byte Stx = 0x02, Address = 0x01;

    public string SetpointLabel => "Volume (mL)";
    public string RateLabel => "Rate (mL/min)";
    public string DisplayLabel => "Dispensed (mL)";
    public IReadOnlyList<string> Modes { get; } = ["Infuse", "Withdraw"];

    private static ushort Crc(ReadOnlySpan<byte> data)
    {
        ushort crc = 0xFFFF;
        foreach (var b in data)
        {
            crc ^= (ushort)(b << 8);
            for (var i = 0; i < 8; i++)
            {
                crc = (crc & 0x8000) != 0 ? (ushort)((crc << 1) ^ 0x1021) : (ushort)(crc << 1);
            }
        }
        return crc;
    }

    private static byte[] Frame(byte opcode, byte[] payload)
    {
        var body = new byte[3 + payload.Length];
        body[0] = Address;
        body[1] = opcode;
        body[2] = (byte)payload.Length;
        payload.CopyTo(body, 3);
        var crc = Crc(body);
        return [Stx, .. body, (byte)(crc >> 8), (byte)crc];
    }

    private static void ReadExactly(Stream s, byte[] buffer, int offset, int count)
    {
        while (count > 0)
        {
            var n = s.Read(buffer, offset, count);
            if (n == 0) throw new IOException("connection closed");
            offset += n;
            count -= n;
        }
    }

    private static byte[] Exchange(Link link, byte opcode, byte[] payload) => link.Exchange(s =>
    {
        var frame = Frame(opcode, payload);
        s.Write(frame, 0, frame.Length);
        s.Flush();
        var header = new byte[4];
        ReadExactly(s, header, 0, 4);
        var rest = new byte[header[3] + 2];
        ReadExactly(s, rest, 0, rest.Length);
        if (rest.Length < 3 || rest[0] != 0)
        {
            throw new InvalidOperationException($"pump status {(rest.Length > 0 ? rest[0] : -1)}");
        }
        return rest[..^2];
    });

    private static byte[] F32(double v) => BitConverter.GetBytes((float)v);

    public string? Connect(Link link) => Poll(link);

    public string? Poll(Link link)
    {
        var payload = Exchange(link, 0x01, []);
        return BitConverter.ToSingle(payload, 2).ToString("F3", CultureInfo.InvariantCulture);
    }

    public string? Apply(Link link, double setpoint, double rate)
    {
        Exchange(link, 0x11, F32(setpoint));
        Exchange(link, 0x10, F32(rate));
        return "Applied";
    }

    public string? SetMode(Link link, int index)
    {
        Exchange(link, 0x12, [(byte)index]);
        return $"Direction {Modes[index]}";
    }

    public string? Start(Link link)
    {
        Exchange(link, 0x20, []);
        return "Running";
    }

    public string? Stop(Link link)
    {
        Exchange(link, 0x21, []);
        return "Stopped";
    }

    public void Disconnect(Link link)
    {
    }
}
