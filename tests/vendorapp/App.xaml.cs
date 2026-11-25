using System.Windows;

namespace Calder.VendorApp;

/// <summary>
/// Command line: --protocol thermostat|pump  (--tcp host:port | --pipe name)  [--poll-ms N]
/// </summary>
public partial class App : Application
{
    private void OnStartup(object sender, StartupEventArgs e)
    {
        var options = Options.Parse(e.Args);
        var window = new MainWindow(options);
        window.Show();
    }
}

public sealed record Options(string Protocol, string? Tcp, string? Pipe, int PollMs)
{
    public static Options Parse(string[] args)
    {
        string protocol = "thermostat";
        string? tcp = null, pipe = null;
        int poll = 1000;
        for (var i = 0; i + 1 < args.Length; i += 2)
        {
            switch (args[i])
            {
                case "--protocol": protocol = args[i + 1]; break;
                case "--tcp": tcp = args[i + 1]; break;
                case "--pipe": pipe = args[i + 1]; break;
                case "--poll-ms": poll = int.Parse(args[i + 1]); break;
            }
        }
        if (tcp == null && pipe == null)
        {
            tcp = "127.0.0.1:5025";
        }
        return new Options(protocol, tcp, pipe, poll);
    }
}
