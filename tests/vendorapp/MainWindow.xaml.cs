using System.ComponentModel;
using System.IO;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace Calder.VendorApp;

/// <summary>
/// A deliberately ordinary vendor UI: labeled inputs, a mode selector, start/stop buttons
/// and a polled display, talking to an instrument simulator over TCP or a named pipe.
/// </summary>
public partial class MainWindow : Window
{
    private readonly Options _options;
    private readonly IInstrumentProtocol _protocol;
    private readonly DispatcherTimer _poll;
    private Link? _link;
    private bool _suppressModeEvent;

    public MainWindow(Options options)
    {
        InitializeComponent();
        _options = options;
        _protocol = options.Protocol == "pump" ? new PumpProtocol() : new ThermostatProtocol();
        SetpointLabel.Content = _protocol.SetpointLabel;
        RateLabel.Content = _protocol.RateLabel;
        DisplayLabel.Content = _protocol.DisplayLabel;
        _suppressModeEvent = true;
        foreach (var mode in _protocol.Modes)
        {
            ModeSelector.Items.Add(new ComboBoxItem { Content = mode, Name = null });
        }
        _suppressModeEvent = false;
        _poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(options.PollMs) };
        _poll.Tick += (_, _) => Run(() => _protocol.Poll(_link!), ShowReading, quiet: true);
        Title = $"Calder Test Vendor App ({options.Protocol})";
    }

    private void Run(Func<string?> action, Action<string?>? onResult = null, bool quiet = false)
    {
        if (_link == null)
        {
            if (!quiet) StatusText.Text = "Not connected";
            return;
        }
        Task.Run(() =>
        {
            try
            {
                var result = action();
                Dispatcher.Invoke(() => onResult?.Invoke(result));
            }
            catch (Exception ex) when (ex is IOException or TimeoutException or InvalidOperationException)
            {
                Dispatcher.Invoke(() => StatusText.Text = $"Error: {ex.Message}");
            }
        });
    }

    private void ShowReading(string? value)
    {
        if (value != null)
        {
            Display.Text = value;
        }
    }

    private static double Number(TextBox box) => double.Parse(box.Text.Replace(',', '.'), CultureInfo.InvariantCulture);

    private void OnConnect(object sender, RoutedEventArgs e)
    {
        try
        {
            _link?.Dispose();
            _link = _options.Pipe != null ? Link.OpenPipe(_options.Pipe) : Link.OpenTcp(_options.Tcp!);
            StatusText.Text = "Connected";
            Run(() => _protocol.Connect(_link), r => StatusText.Text = $"Connected: {r}");
            _poll.Start();
        }
        catch (Exception ex) when (ex is IOException or TimeoutException or System.Net.Sockets.SocketException)
        {
            StatusText.Text = $"Connect failed: {ex.Message}";
        }
    }

    private void OnDisconnect(object sender, RoutedEventArgs e) => Disconnect();

    private void Disconnect()
    {
        _poll.Stop();
        var link = _link;
        if (link == null) return;
        try
        {
            _protocol.Disconnect(link);
        }
        catch (Exception ex) when (ex is IOException or TimeoutException)
        {
            StatusText.Text = ex.Message;
        }
        link.Dispose();
        _link = null;
        StatusText.Text = "Disconnected";
    }

    private void OnApply(object sender, RoutedEventArgs e)
    {
        double setpoint, rate;
        try
        {
            setpoint = Number(SetpointBox);
            rate = Number(RateBox);
        }
        catch (FormatException)
        {
            StatusText.Text = "Invalid number";
            return;
        }
        Run(() => _protocol.Apply(_link!, setpoint, rate), r => StatusText.Text = r ?? "Applied");
    }

    private void OnModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressModeEvent || ModeSelector.SelectedIndex < 0) return;
        var index = ModeSelector.SelectedIndex;
        Run(() => _protocol.SetMode(_link!, index), r => StatusText.Text = r ?? "Mode set");
    }

    private void OnStart(object sender, RoutedEventArgs e) => Run(() => _protocol.Start(_link!), r => StatusText.Text = r ?? "Started");

    private void OnStop(object sender, RoutedEventArgs e) => Run(() => _protocol.Stop(_link!), r => StatusText.Text = r ?? "Stopped");

    protected override void OnClosing(CancelEventArgs e)
    {
        Disconnect();
        base.OnClosing(e);
    }
}
