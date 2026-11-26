using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Apollo.Neb.Desktop;

public sealed partial class MainWindow
{
    private readonly FleetLink _fleet = new(FleetCredentialStore.ForCurrentUser(),
        typeof(App).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()!
            .InformationalVersion.Split('+')[0]);
    private Task? _fleetActivity;
    private Task<NvidiaSnapshot>? _gpuRead;
    private bool _fleetLoaded;
    private bool _fleetBusy;
    private bool _fleetConfirmed;
    private bool _powerRangeAvailable;
    private readonly NvidiaPowerAuthority _powerAuthority;
    private readonly NvidiaPowerControl _powerControl;

    private Task InitializeFleetAsync() => RunFleetAsync(async token =>
    {
        await _fleet.LoadAsync(token);
        _fleetLoaded = true;
        SetFleetMessage(_fleet.Current?.Stage switch
        {
            FleetStage.Pending => "Привязка ожидает подтверждения. Повторите ввод ключа.",
            FleetStage.Rejected => "Панель отклонила ключ. Запросите новую привязку.",
            FleetStage.Verified => "Проверяем связь с панелью…",
            _ => "Привязка не настроена",
        });
        if (_fleet.Current?.Stage == FleetStage.Verified)
            await ReportGpuAsync((await ReadGpuAsync(token)).Devices, check: true, token);
    });

    private async void PairFleet_Click(object sender, RoutedEventArgs args)
    {
        if (!FleetLink.TryOrigin(FleetOriginBox.Text, out var origin))
        {
            SetFleetMessage("Введите HTTPS-адрес панели без пути.");
            return;
        }
        string invitation = FleetKeyBox.Password;
        if (!FleetLink.IsInvitation(invitation))
        {
            SetFleetMessage("Вставьте составной одноразовый ключ из раздела «Ноды».");
            return;
        }
        if (FleetConsent.IsChecked != true)
        {
            SetFleetMessage("Подтвердите отправку показаний GPU.");
            return;
        }

        FleetKeyBox.Password = "";
        await RunFleetAsync(async token =>
        {
            SetFleetMessage("Привязываем ПК и проверяем первый замер…");
            var gpu = await ReadGpuAsync(token);
            long time = await _fleet.PairAsync(origin, invitation, gpu.Devices, token);
            ShowFleetSent(time, gpu.Devices.Count);
        });
    }

    private async void CheckFleet_Click(object sender, RoutedEventArgs args) =>
        await RunFleetAsync(async token =>
        {
            SetFleetMessage("Проверяем связь с панелью…");
            await ReportGpuAsync((await ReadGpuAsync(token)).Devices, check: true, token);
        });

    private async void ForgetFleet_Click(object sender, RoutedEventArgs args) =>
        await RunFleetAsync(token =>
        {
            token.ThrowIfCancellationRequested();
            _powerAuthority.Allow(false);
            _powerControl.Reset();
            _fleet.Forget();
            _fleetLoaded = true;
            _fleetConfirmed = false;
            FleetConsent.IsChecked = false;
            FleetKeyBox.Password = "";
            FleetOriginBox.Text = "";
            FleetLastSent.Text = "";
            SetFleetMessage("Локальный ключ удалён. Отозвать его можно в разделе «Ноды» панели.");
            return Task.CompletedTask;
        });

    private Task SendFleetAsync(NvidiaSnapshot gpu) =>
        _fleet.Current?.Stage == FleetStage.Verified
            ? RunFleetAsync(token => ReportGpuAsync(gpu.Devices, check: !_fleetConfirmed, token))
            : Task.CompletedTask;

    private async Task ReportGpuAsync(IReadOnlyList<NvidiaGpuReading> devices, bool check, CancellationToken token)
    {
        long time = await _fleet.ReportAsync(devices, check, token, _powerControl);
        ShowFleetSent(time, devices.Count);
    }

    private async Task<NvidiaSnapshot> ReadGpuAsync(CancellationToken token)
    {
        var read = _gpuRead ??= NvidiaTelemetry.ReadAsync(token);
        try
        {
            var sample = await read;
            _powerRangeAvailable = sample.Devices.Any(device => device.PowerLimit is { IsValid: true });
            return sample;
        }
        finally
        {
            if (ReferenceEquals(_gpuRead, read)) _gpuRead = null;
        }
    }

    private Task RunFleetAsync(Func<CancellationToken, Task> action)
    {
        if (_fleetBusy || _shutdown.IsCancellationRequested) return Task.CompletedTask;
        _fleetActivity = ExecuteFleetAsync(action);
        return _fleetActivity;
    }

    private async Task ExecuteFleetAsync(Func<CancellationToken, Task> action)
    {
        _fleetBusy = true;
        RenderFleetControls();
        try { await action(_shutdown.Token); }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
            or OperationCanceledException or CryptographicException or UnauthorizedAccessException or ArgumentException
            or InvalidOperationException or System.Runtime.InteropServices.COMException)
        {
            _fleetConfirmed = false;
            if (!_shutdown.IsCancellationRequested)
            {
                SetFleetMessage(error switch
                {
                    _ when _fleet.Current?.Stage == FleetStage.Rejected => "Панель отклонила ключ. Запросите новую привязку.",
                    HttpRequestException { StatusCode: HttpStatusCode.Unauthorized } =>
                        "Ключ привязки отклонён. Проверьте код и срок действия.",
                    _ when !_fleetLoaded => "Сохранённая привязка недоступна. Проверьте, открыто ли другое окно.",
                    ArgumentException => "Адрес или ключ отличается от сохранённой попытки.",
                    _ when _fleet.Current?.Stage == FleetStage.Pending =>
                        "Привязка не подтверждена. Повторите с тем же ключом.",
                    _ => "Панель не подтвердила связь.",
                });
            }
        }
        finally
        {
            _fleetBusy = false;
            if (!_shutdown.IsCancellationRequested) RenderFleetControls();
        }
    }

    private void ShowFleetSent(long time, int deviceCount)
    {
        if (_shutdown.IsCancellationRequested) return;
        _fleetConfirmed = true;
        SetFleetMessage(deviceCount > 0 ? "Связь подтверждена. Показания GPU отправляются."
            : "Связь подтверждена. Нет данных GPU.", ready: true);
        FleetLastSent.Text = "Последняя отправка " +
            DateTimeOffset.FromUnixTimeSeconds(time).ToLocalTime().ToString("HH:mm:ss", DisplayCulture);
    }

    private void SetFleetMessage(string text, bool ready = false)
    {
        if (FleetLinkMessage.Text != text) FleetLinkMessage.Text = text;
        FleetLinkMessage.Foreground = (Brush)Root.Resources[ready ? "NebMint" : "NebMuted"];
    }

    private void PowerConsent_Changed(object sender, RoutedEventArgs args)
    {
        _powerAuthority.Allow(FleetPowerConsent.IsChecked == true && _fleet.Current?.Stage == FleetStage.Verified);
        RenderPowerControls();
    }

    private void RenderPowerControls()
    {
        bool linked = _fleet.Current?.Stage == FleetStage.Verified;
        if (!linked && _powerAuthority.Granted) _powerAuthority.Allow(false);
        if (!linked && FleetPowerConsent.IsChecked == true) FleetPowerConsent.IsChecked = false;
        FleetPowerControls.Visibility = linked ? Visibility.Visible : Visibility.Collapsed;
        FleetPowerConsent.IsEnabled = linked && _powerAuthority.Elevated;
        FleetPowerStatus.Text = !_powerAuthority.Elevated
            ? "Для изменения лимита приложение должно быть запущено с правами администратора."
            : !_powerAuthority.Granted ? "Изменение лимита выключено. Разрешение действует до закрытия окна."
            : !_powerRangeAvailable ? "Ждём доступный диапазон мощности от NVIDIA."
            : "Разрешение действует до закрытия окна.";
        if (_powerAuthority.WriteStarted)
        {
            ShowPowerOutcome("Команда уже передана драйверу. Проверяем результат.");
            return;
        }
        string? outcome = _powerControl.LastAttempt switch
        {
            { Phase: "applied", Confirmed: true } attempt =>
                $"Панель подтвердила лимит {Format(attempt.ObservedWatts, " Вт")}.",
            { Phase: "refused", Confirmed: true } => "Команда отклонена без изменения лимита.",
            { Phase: "refused" } => "Панель не подтвердила запуск команды.",
            not null => "Результат команды не подтверждён.",
            _ => null,
        };
        ShowPowerOutcome(outcome);
    }

    private void ShowPowerOutcome(string? outcome)
    {
        FleetPowerOutcome.Text = outcome ?? "";
        FleetPowerOutcome.Visibility = outcome is null ? Visibility.Collapsed : Visibility.Visible;
    }

    private void RenderFleetControls()
    {
        var identity = _fleet.Current;
        bool verified = identity?.Stage == FleetStage.Verified;
        bool pairing = _fleetLoaded && (identity is null or { Stage: FleetStage.Pending });
        FleetSetup.Visibility = pairing ? Visibility.Visible : Visibility.Collapsed;
        FleetPairButton.Content = identity?.Stage == FleetStage.Pending ? "Повторить привязку" : "Привязать ПК";
        FleetConnectInfo.Visibility = identity is not null ? Visibility.Visible : Visibility.Collapsed;
        FleetOriginBox.IsReadOnly = identity is not null;
        FleetCheckButton.Visibility = verified ? Visibility.Visible : Visibility.Collapsed;
        FleetForgetButton.Visibility = identity is not null || !_fleetLoaded && !_fleetBusy
            ? Visibility.Visible : Visibility.Collapsed;
        FleetOriginBox.IsEnabled = !_fleetBusy;
        FleetKeyBox.IsEnabled = !_fleetBusy;
        FleetConsent.IsEnabled = !_fleetBusy;
        FleetPairButton.IsEnabled = !_fleetBusy;
        FleetCheckButton.IsEnabled = !_fleetBusy;
        FleetForgetButton.IsEnabled = !_fleetBusy;
        FleetLastSent.Visibility = verified ? Visibility.Visible : Visibility.Collapsed;
        RenderPowerControls();
        if (identity is null) return;
        var origin = new Uri(identity.Origin);
        FleetOriginBox.Text = origin.GetLeftPart(UriPartial.Authority);
        FleetConnectedOrigin.Content = origin.Authority;
        FleetConnectedOrigin.NavigateUri = origin;
    }
}
