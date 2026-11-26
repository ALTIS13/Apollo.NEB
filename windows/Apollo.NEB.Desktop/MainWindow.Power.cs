using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using Microsoft.UI.Xaml;

namespace Apollo.Neb.Desktop;

public sealed record LocalPowerPreset(string Label, int? Watts);

public sealed class LocalGpuCard(string id) : INotifyPropertyChanged
{
    private static readonly LocalPowerPreset Custom = new("Свой лимит", null);
    private NvidiaGpuReading? _reading;
    private PowerRange? _range;
    private bool _rangeAvailable;
    private double? _currentLimit;
    private LocalPowerPreset[] _presets = [Custom];
    private LocalPowerPreset _preset = Custom;
    private string _watts = "";
    private string _outcome = "";
    private bool _syncingChoice;
    private bool _elevated;
    private bool _localControl;
    private bool _busy;
    private bool _applying;

    public event PropertyChangedEventHandler? PropertyChanged;
    internal string Id { get; } = id;
    public string Name { get; private set; } = "";
    public string Pci => "PCI " + Id;
    public string Temperature => MainWindow.Format(_reading?.Temperature, " °C");
    public string Utilization => MainWindow.Format(_reading?.Utilization, "%");
    public string Memory => _reading is { } reading && (reading.MemoryUsedMiB is not null || reading.MemoryTotalMiB is not null)
        ? $"{MainWindow.FormatCount(reading.MemoryUsedMiB)} / {MainWindow.FormatCount(reading.MemoryTotalMiB)} МиБ"
        : MainWindow.Format(null, "");
    public string Watts => MainWindow.Format(_reading?.Watts, " Вт");
    public string CurrentLimit => MainWindow.Format(_currentLimit, " Вт");
    public IReadOnlyList<LocalPowerPreset> Presets => _presets;
    public Visibility PickerVisibility => _range is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility PresetsVisibility => _presets.Length > 1 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility OutcomeVisibility => _outcome.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    public string Outcome => _outcome;
    public string ApplyText => _applying ? "Применяем…" : "Применить";
    public bool CanEdit => !_applying;
    public bool CanApply => _rangeAvailable && _localControl && _elevated && !_busy
        && TryWatts(out int watts) && _currentLimit is double current && current != watts;

    public LocalPowerPreset SelectedPreset
    {
        get => _preset;
        set
        {
            if (_syncingChoice || value is null || ReferenceEquals(value, _preset) || !_presets.Contains(value)) return;
            Select(value.Watts?.ToString(CultureInfo.InvariantCulture) ?? _watts, value);
        }
    }

    public string WattInput
    {
        get => _watts;
        set
        {
            if (_syncingChoice || value == _watts) return;
            Select(value, FindPreset(value));
        }
    }

    public string RangeText => _rangeAvailable && _range is { } range
        ? $"Целые значения {range.Minimum}–{range.Maximum} Вт"
        : "";

    public string Help => !_rangeAvailable ? "NVIDIA не предоставила допустимый диапазон."
        : !_elevated ? "Для применения запустите Apollo.NEB от имени администратора."
        : !_localControl ? "Изменение недоступно. Проверьте драйвер NVIDIA."
        : _currentLimit is null ? "Текущий лимит неизвестен. Дождитесь следующего замера."
        : !TryWatts(out _) ? "Введите целое число из допустимого диапазона."
        : "";

    internal bool TryWatts(out int watts) =>
        ParseWatts(_watts, out watts) && _range is { } range && watts >= range.Minimum && watts <= range.Maximum;

    private static bool ParseWatts(string text, out int watts) =>
        int.TryParse(text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out watts);

    internal void Refresh(NvidiaGpuReading? reading, bool acceptLimit)
    {
        if (reading is not null && Name != reading.Name)
        {
            Name = reading.Name;
            Changed(nameof(Name));
        }
        if (_reading != reading)
        {
            _reading = reading;
            Changed(nameof(Temperature), nameof(Utilization), nameof(Memory), nameof(Watts));
        }
        if (!acceptLimit) return;

        var limit = reading?.PowerLimit;
        PowerRange? range = PowerRange.From(limit);
        bool wasAvailable = _rangeAvailable;
        double? previous = _currentLimit;
        _rangeAvailable = range is not null;
        _currentLimit = limit is { IsValid: true } ? limit.Current : null;
        if (range is { } known && _range != known)
        {
            if (_range is null && _currentLimit is double current
                && current == Math.Truncate(current) && current >= known.Minimum && current <= known.Maximum)
                _watts = ((int)current).ToString(CultureInfo.InvariantCulture);
            _range = known;
            _presets = [.. BuildPresets(known), Custom];
            _preset = FindPreset(_watts);
            NotifyChoice(nameof(Presets), nameof(PresetsVisibility), nameof(PickerVisibility));
        }
        if (wasAvailable != _rangeAvailable || previous != _currentLimit)
            Changed(nameof(CurrentLimit), nameof(RangeText), nameof(CanApply), nameof(Help));
    }

    internal void SetControls(bool elevated, bool localControl, bool busy, bool applying)
    {
        if ((_elevated, _localControl, _busy, _applying) == (elevated, localControl, busy, applying)) return;
        (_elevated, _localControl, _busy, _applying) = (elevated, localControl, busy, applying);
        Changed(nameof(CanApply), nameof(CanEdit), nameof(ApplyText), nameof(Help));
    }

    internal void ClearOutcome() => ShowOutcome("");

    internal void Finish(int watts, NvidiaPowerAttempt result)
    {
        double? observed = result.ObservedWatts is double number && double.IsFinite(number) && number is >= 1 and <= 3000
            ? number : null;
        bool applied = result.Phase == "applied" && observed == watts;
        if (observed is not null || result.Phase is "unknown" or "applied")
        {
            _currentLimit = observed;
            Changed(nameof(CurrentLimit), nameof(CanApply), nameof(Help));
        }
        ShowOutcome(result.Phase switch
        {
            "applied" when applied => $"Применено. Драйвер подтвердил {watts} Вт.",
            "refused" => "Запрос отклонён.",
            "busy" => "Выполняется другая команда. Повторите после её завершения.",
            _ => "Результат не подтверждён. Проверьте текущий лимит.",
        });
    }

    private void Select(string watts, LocalPowerPreset preset)
    {
        bool textChanged = _watts != watts;
        bool profileChanged = !ReferenceEquals(_preset, preset);
        _watts = watts;
        _preset = preset;
        if (textChanged || profileChanged)
        {
            ClearOutcome();
            _syncingChoice = true;
            try
            {
                if (textChanged) Changed(nameof(WattInput));
                if (profileChanged) Changed(nameof(SelectedPreset));
            }
            finally { _syncingChoice = false; }
            Changed(nameof(CanApply), nameof(Help));
        }
    }

    private void NotifyChoice(params string[] properties)
    {
        _syncingChoice = true;
        try
        {
            Changed(properties);
            Changed(nameof(WattInput), nameof(SelectedPreset), nameof(RangeText), nameof(CanApply), nameof(Help));
        }
        finally { _syncingChoice = false; }
    }

    private LocalPowerPreset FindPreset(string text) =>
        ParseWatts(text, out int watts) ? _presets.FirstOrDefault(preset => preset.Watts == watts) ?? Custom : Custom;

    private void ShowOutcome(string message)
    {
        if (_outcome == message) return;
        _outcome = message;
        Changed(nameof(Outcome), nameof(OutcomeVisibility));
    }

    private void Changed(params string[] properties)
    {
        foreach (string property in properties)
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(property));
    }

    private static IEnumerable<LocalPowerPreset> BuildPresets(PowerRange range)
    {
        if (range.Factory is not double factory) yield break;
        int lowestPercent = 100;
        int previous = 0;
        for (int percent = 60; percent < 100; percent++)
        {
            if (range.PresetWatts(factory, percent) is not int watts || watts >= factory) continue;
            lowestPercent = percent;
            previous = watts;
            yield return new($"За ПК · {percent}% · {watts} Вт", watts);
            break;
        }
        foreach (int percent in new[] { 80, 90 })
        {
            if (percent <= lowestPercent || range.PresetWatts(factory, percent) is not int watts
                || watts <= previous || watts >= factory) continue;
            previous = watts;
            yield return new($"{percent}% · {watts} Вт", watts);
        }
        if (factory == Math.Truncate(factory) && range.PresetWatts(factory, 100) is int standard)
            yield return new($"Штатный · 100% · {standard} Вт", standard);
    }

    private readonly record struct PowerRange(double Min, double Max, double? Factory)
    {
        public int Minimum => (int)Math.Ceiling(Min);
        public int Maximum => (int)Math.Floor(Max);

        public static PowerRange? From(NvidiaPowerLimit? limit)
        {
            if (limit is not { IsValid: true }) return null;
            double? factory = limit.DefaultWatts is double watts && double.IsFinite(watts)
                && watts >= limit.Min && watts <= limit.Max ? watts : null;
            var range = new PowerRange(limit.Min, limit.Max, factory);
            return range.Minimum <= range.Maximum ? range : null;
        }

        public int? PresetWatts(double factory, int percent)
        {
            double nominal = factory * percent / 100;
            int watts = (int)Math.Round(nominal, MidpointRounding.AwayFromZero);
            return nominal >= Min && nominal <= Max && watts >= Minimum && watts <= Maximum ? watts : null;
        }
    }
}

public sealed partial class MainWindow
{
    private readonly ObservableCollection<LocalGpuCard> _localGpus = [];
    private LocalGpuCard? _applyingPower;
    private Task? _localPowerActivity;
    private long _powerReadRevision;
    private Task<NvidiaSnapshot>? _priorPowerRead;
    private bool _localControlAvailable;

    private void RenderLocalGpuCards(NvidiaSnapshot sample, bool acceptLimit)
    {
        if (sample.State == NvidiaQueryState.Ready)
        {
            var present = new HashSet<string>(StringComparer.Ordinal);
            var previous = _localGpus.ToDictionary(card => card.Id, StringComparer.Ordinal);
            foreach (var reading in sample.Devices)
            {
                string id = NvidiaTelemetry.CanonicalAddress(reading.BusId);
                if (!present.Add(id)) continue;
                if (!previous.TryGetValue(id, out var card))
                {
                    card = new LocalGpuCard(id);
                    _localGpus.Add(card);
                }
                card.Refresh(reading, acceptLimit && card != _applyingPower);
            }
            for (int index = _localGpus.Count - 1; index >= 0; index--)
            {
                if (!present.Contains(_localGpus[index].Id)) _localGpus.RemoveAt(index);
            }
        }
        else
        {
            foreach (var card in _localGpus) card.Refresh(null, acceptLimit && card != _applyingPower);
        }
        _localControlAvailable = _powerControl.CanApplyLocally;
        UpdateLocalPowerControls();
    }

    private void UpdateLocalPowerControls()
    {
        foreach (var card in _localGpus)
            card.SetControls(_powerAuthority.Elevated, _localControlAvailable,
                             _applyingPower is not null, _applyingPower == card);
    }

    private async void ApplyLocalPower_Click(object sender, RoutedEventArgs args)
    {
        if (sender is not FrameworkElement { DataContext: LocalGpuCard card }
            || _shutdown.IsCancellationRequested || _applyingPower is not null || !_localGpus.Contains(card)
            || !card.CanApply || !card.TryWatts(out int watts)) return;

        _localPowerActivity = ApplyLocalPowerAsync(card, watts);
        await _localPowerActivity;
    }

    private async Task ApplyLocalPowerAsync(LocalGpuCard card, int watts)
    {
        _applyingPower = card;
        _powerReadRevision++;
        card.ClearOutcome();
        UpdateLocalPowerControls();
        try
        {
            var result = await _powerControl.ApplyLocalAsync(card.Id, watts, _shutdown.Token);
            if (!_shutdown.IsCancellationRequested) card.Finish(watts, result);
        }
        catch (OperationCanceledException) when (_shutdown.IsCancellationRequested) { }
        catch (Exception error) when (error is OperationCanceledException or IOException or UnauthorizedAccessException
            or InvalidOperationException or Win32Exception)
        {
            if (!_shutdown.IsCancellationRequested) card.Finish(watts, new("unknown", null, false));
        }
        finally
        {
            _priorPowerRead = _gpuRead;
            _powerReadRevision++;
            _applyingPower = null;
            if (!_shutdown.IsCancellationRequested) UpdateLocalPowerControls();
        }
    }
}
