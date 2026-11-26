using System.Globalization;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Windows.Graphics;
using Launcher = Windows.System.Launcher;

namespace Apollo.Neb.Desktop;

public sealed partial class MainWindow : Window
{
    private const string Missing = "—";
    private static readonly CultureInfo DisplayCulture = CultureInfo.GetCultureInfo("ru-RU");
    private static readonly TimeSpan StaleAfter = TimeSpan.FromMinutes(2);
    private readonly LocalPanel _client = new();
    private readonly CancellationTokenSource _shutdown = new();
    private readonly DispatcherQueueTimer _timer;
    private readonly DispatcherQueueTimer _gpuTimer;
    private PublicStatus? _last;
    private int _port;
    private bool _reading;
    private bool _gpuReading;

    public MainWindow(int port)
    {
        _powerAuthority = NvidiaPowerAuthority.ForCurrentProcess();
        _powerControl = new(_powerAuthority, new NvidiaTelemetry.PowerAdapter());
        InitializeComponent();
        LocalGpuCards.ItemsSource = _localGpus;
        _port = port;
        PortBox.Text = _port.ToString(CultureInfo.InvariantCulture);
        SetEndpoint();
        AppWindow.Resize(new SizeInt32(940, 920));
        SetTitleBar();
        _timer = DispatcherQueue.CreateTimer();
        _timer.Interval = TimeSpan.FromSeconds(5);
        _timer.Tick += async (_, _) => await RefreshAsync(_port);
        _gpuTimer = DispatcherQueue.CreateTimer();
        _gpuTimer.Interval = TimeSpan.FromSeconds(10);
        _gpuTimer.Tick += async (_, _) => await RefreshLocalGpuAsync();
        Closed += async (_, _) =>
        {
            _timer.Stop();
            _gpuTimer.Stop();
            _powerAuthority.Allow(false);
            _shutdown.Cancel();
            _client.Dispose();
            if (_fleetActivity is not null) await _fleetActivity;
            if (_localPowerActivity is not null) await _localPowerActivity;
            _fleet.Dispose();
            _shutdown.Dispose();
        };
        Root.Loaded += async (_, _) =>
        {
            _timer.Start();
            _gpuTimer.Start();
            await Task.WhenAll(RefreshAsync(_port), RefreshLocalGpuAsync(), InitializeFleetAsync());
        };
    }

    private void SetTitleBar()
    {
        if (!AppWindowTitleBar.IsCustomizationSupported()) return;
        var bar = AppWindow.TitleBar;
        bar.BackgroundColor = ColorHelper.FromArgb(255, 12, 20, 26);
        bar.ForegroundColor = Colors.White;
        bar.ButtonBackgroundColor = bar.BackgroundColor;
        bar.ButtonForegroundColor = bar.ForegroundColor;
    }

    private void SetEndpoint() => EndpointValue.Text = LocalPanel.HtmlUri(_port).Authority;

    private async void Refresh_Click(object sender, RoutedEventArgs args)
    {
        if (!LocalPanel.TryPort(PortBox.Text, out var port))
        {
            PortError.Text = "Введите порт от 1 до 65535.";
            PortError.Visibility = Visibility.Visible;
            return;
        }

        PortError.Visibility = Visibility.Collapsed;
        await RefreshAsync(port);
    }

    private async void OpenPanel_Click(object sender, RoutedEventArgs args)
    {
        if (!await Launcher.LaunchUriAsync(LocalPanel.HtmlUri(_port)))
        {
            PortError.Text = "Не удалось открыть панель в браузере.";
            PortError.Visibility = Visibility.Visible;
        }
    }

    private async Task RefreshAsync(int port)
    {
        if (_reading || _shutdown.IsCancellationRequested) return;
        _reading = true;
        RefreshButton.IsEnabled = false;
        if (port != _port)
        {
            _port = port;
            _last = null;
            OpenPanelButton.IsEnabled = false;
            SetEndpoint();
            Render(null, "Проверяем локальную панель…");
        }

        try
        {
            var sample = await _client.ReadAsync(port, _shutdown.Token);
            if (_shutdown.IsCancellationRequested) return;
            _last = sample;
            OpenPanelButton.IsEnabled = true;
            bool stale = DateTimeOffset.UtcNow - sample.ObservedAt > StaleAfter;
            Render(sample, stale ? "Данные устарели" : "Панель на связи");
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or OperationCanceledException)
        {
            if (_shutdown.IsCancellationRequested) return;
            OpenPanelButton.IsEnabled = false;
            string state = error is InvalidDataException
                ? "Панель вернула неподдерживаемые данные"
                : "Связь с локальной панелью недоступна";
            Render(_last, state);
        }
        finally
        {
            _reading = false;
            if (!_shutdown.IsCancellationRequested) RefreshButton.IsEnabled = true;
        }
    }

    private async Task RefreshLocalGpuAsync()
    {
        if (_gpuReading || _shutdown.IsCancellationRequested
            || AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized }
                && _fleet.Current?.Stage != FleetStage.Verified) return;
        _gpuReading = true;
        long powerRevision = _powerReadRevision;
        var sharedRead = _gpuRead;
        try
        {
            var sample = await ReadGpuAsync(_shutdown.Token);
            if (_shutdown.IsCancellationRequested) return;
            LocalGpuState.Text = sample.State switch
            {
                NvidiaQueryState.Ready => $"NVIDIA · устройств: {sample.Devices.Count}",
                NvidiaQueryState.NoDevices => "Устройства NVIDIA не обнаружены.",
                NvidiaQueryState.ToolMissing => "Установленная утилита nvidia-smi не найдена.",
                _ => "Локальные датчики NVIDIA недоступны.",
            };
            bool acceptLimit = powerRevision == _powerReadRevision
                && (sharedRead is null || !ReferenceEquals(sharedRead, _priorPowerRead));
            if (acceptLimit) _priorPowerRead = null;
            RenderLocalGpuCards(sample, acceptLimit);
            await SendFleetAsync(sample);
        }
        catch (OperationCanceledException) when (_shutdown.IsCancellationRequested) { }
        finally
        {
            _gpuReading = false;
        }
    }

    private void Render(PublicStatus? value, string state)
    {
        ConnectionState.Text = state;
        ObservedValue.Text = value is null
            ? "Запустите локальную панель Apollo.NEB и проверьте порт."
            : "Последний замер " + value.ObservedAt.ToLocalTime().ToString("dd MMM, HH:mm:ss", DisplayCulture);
        RateValue.Text = FormatRate(value?.Hashrate);
        WorkerValue.Text = value is null ? Missing : $"{value.Running} / {value.Total}";
        AttentionValue.Text = value is { Attention: > 0 } ? $"Требуют внимания: {value.Attention}" : "";
        WattsValue.Text = Format(value?.Watts, " Вт");
        SharesValue.Text = value is null ? Missing : $"{FormatCount(value.Accepted)} / {FormatCount(value.Rejected)}";
        EdgeValue.Text = Format(value?.Edge, " °C");
        HotValue.Text = Format(value?.Hotspot, " °C");
        MemoryValue.Text = Format(value?.Memory, " °C");
        FeeValue.Text = value is null ? Missing : FormatFee(value);
        FeeRate.Text = value is { FeeEnabled: true }
            ? Format(value.FeeBasisPoints / 100.0, "% активной добычи")
            : "";
    }

    internal static string Format(double? value, string suffix) =>
        value is double number ? number.ToString("0.##", DisplayCulture) + suffix : Missing;

    internal static string FormatCount(long? value) =>
        value?.ToString("N0", DisplayCulture) ?? Missing;

    private static string FormatRate(double? rate) => rate switch
    {
        >= 1_000_000_000 => Format(rate / 1_000_000_000, " GH/s"),
        >= 1_000_000 => Format(rate / 1_000_000, " MH/s"),
        >= 1_000 => Format(rate / 1_000, " kH/s"),
        _ => Format(rate, " H/s"),
    };

    private static string FormatFee(PublicStatus value) => !value.FeeEnabled ? "Выключен" : value.FeeState switch
    {
        "active" => "Идёт окно DevFee",
        "counting" => "Учитывается время добычи",
        "idle" => "Нет активной добычи",
        "paused" => "На паузе",
        "unavailable" => "Политика недоступна",
        _ => "Состояние не определено",
    };
}
