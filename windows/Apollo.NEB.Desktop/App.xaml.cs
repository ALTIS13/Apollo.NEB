using Microsoft.UI.Xaml;

namespace Apollo.Neb.Desktop;

public partial class App : Application
{
    private MainWindow? _window;

    public App() => InitializeComponent();

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        var port = Environment.GetCommandLineArgs() is [_, "--port", var value]
            && LocalPanel.TryPort(value, out var requested) ? requested : LocalPanel.DefaultPort;
        _window = new MainWindow(port);
        _window.Activate();
    }
}
