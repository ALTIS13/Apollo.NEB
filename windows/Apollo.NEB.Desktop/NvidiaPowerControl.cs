using System.Diagnostics;
using System.Net;
using System.Security.Principal;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Apollo.Neb.Desktop;

internal sealed record NvidiaPowerLimit(
    [property: JsonPropertyName("current")] double Current,
    [property: JsonPropertyName("min")] double Min,
    [property: JsonPropertyName("max")] double Max,
    [property: JsonPropertyName("default_watts"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    double? DefaultWatts = null)
{
    internal bool IsValid => Min >= 1 && Min <= Current && Current <= Max && Max <= 3000;
}

internal sealed class NvidiaPowerAuthority(bool elevated)
{
    private readonly object _gate = new();
    private bool _enabled;
    private bool _started;
    private long _consentVersion;

    internal bool Elevated { get; } = elevated;
    internal long? ConsentVersion
    {
        get { lock (_gate) return Elevated && _enabled ? _consentVersion : null; }
    }
    internal bool Granted => ConsentVersion is not null;

    internal static NvidiaPowerAuthority ForCurrentProcess()
    {
        using var identity = WindowsIdentity.GetCurrent();
        return new(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator));
    }

    internal void Allow(bool enabled)
    {
        lock (_gate)
        {
            bool next = enabled && Elevated;
            if (_enabled != next) _consentVersion++;
            _enabled = next;
        }
    }

    internal bool WriteStarted
    {
        get { lock (_gate) return _started; }
    }

    internal bool TryBegin(NvidiaPowerLease lease, Func<bool> start)
    {
        lock (_gate)
        {
            if (!Elevated || !_enabled || _consentVersion != lease.ConsentVersion || !lease.Active || !start())
                return false;
            _started = true;
            return true;
        }
    }

    internal void EndAttempt()
    {
        lock (_gate) _started = false;
    }
}

internal readonly record struct NvidiaPowerLease(long ConsentVersion, long Expires, long ClaimTimestamp)
{
    internal bool Active =>
        NvidiaPowerControl.Now() < Expires && Stopwatch.GetElapsedTime(ClaimTimestamp) < TimeSpan.FromSeconds(15);
}

internal enum NvidiaPowerWrite { NotStarted, Completed, Unknown }

internal interface INvidiaPowerAdapter
{
    bool Available { get; }
    Task<NvidiaPowerLimit?> ReadAsync(string pci, CancellationToken token);
    Task<NvidiaPowerWrite> WriteAsync(string pci, int watts, Func<Func<bool>, bool> permitStart,
        CancellationToken token);
}

internal sealed record NvidiaPowerAttempt(string Phase, double? ObservedWatts, bool Confirmed);

internal sealed class NvidiaPowerControl(NvidiaPowerAuthority authority, INvidiaPowerAdapter adapter)
{
    private static readonly Regex JobId = new(@"\A[0-9a-f]{32}\z");
    private static readonly Regex CanonicalPci = new(@"\A[0-9a-f]{8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\z");
    private readonly Dictionary<string, long> _attempted = new();
    private readonly SemaphoreSlim _operation = new(1, 1);

    internal NvidiaPowerAttempt? LastAttempt { get; private set; }
    internal bool CanApplyLocally => authority.Elevated && adapter.Available;
    internal long? OfferedConsent =>
        authority.ConsentVersion is long version && adapter.Available ? version : null;

    internal static bool IsGpu(string value) => CanonicalPci.IsMatch(value);
    internal static long Now() => DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    internal void Reset() => LastAttempt = null;

    internal async Task<NvidiaPowerAttempt> ApplyLocalAsync(string pci, int watts, CancellationToken token)
    {
        if (token.IsCancellationRequested || !IsGpu(pci) || watts is < 1 or > 3000 || !CanApplyLocally)
            return new("refused", null, false);
        if (!_operation.Wait(0)) return new("busy", null, false);
        try
        {
            var result = await ApplyAsync(pci, watts, () => authority.Elevated,
                start => authority.Elevated && start(), token).ConfigureAwait(false);
            return result with { Confirmed = result.Phase == "applied" };
        }
        finally { _operation.Release(); }
    }

    internal async Task RunAsync(IReadOnlySet<string> advertised, long consentVersion,
        Func<object?, CancellationToken, Task<JsonDocument>> exchange, CancellationToken token)
    {
        if (authority.ConsentVersion != consentVersion || !_operation.Wait(0)) return;
        try { await RunRemoteAsync(advertised, consentVersion, exchange, token).ConfigureAwait(false); }
        finally
        {
            authority.EndAttempt();
            _operation.Release();
        }
    }

    private async Task RunRemoteAsync(IReadOnlySet<string> advertised, long consentVersion,
        Func<object?, CancellationToken, Task<JsonDocument>> exchange, CancellationToken token)
    {
        Job? job;
        try
        {
            using var reply = await exchange(null, token).ConfigureAwait(false);
            job = Parse(reply.RootElement);
        }
        catch (Exception error) when (IsUnconfirmed(error, token)) { return; }

        long now = Now();
        foreach (var expired in _attempted.Where(item => item.Value <= now).ToArray()) _attempted.Remove(expired.Key);
        if (job is null || job.Expires <= now || !advertised.Contains(job.Gpu)
            || authority.ConsentVersion != consentVersion || !_attempted.TryAdd(job.Id, job.Expires))
            return;

        LastAttempt = new("refused", null, false);
        long claimTimestamp = Stopwatch.GetTimestamp();
        long latestStart = Now() + 15;
        NvidiaPowerLease lease;
        try
        {
            using var claim = await exchange(new { id = job.Id, phase = "claim" }, token).ConfigureAwait(false);
            if (!TryClaimLease(claim.RootElement, out long expires)) return;
            lease = new(consentVersion, expires, claimTimestamp);
            _attempted[job.Id] = Math.Max(_attempted[job.Id], Math.Min(expires, latestStart));
        }
        catch (Exception error) when (IsUnconfirmed(error, token)) { return; }

        try
        {
            var measured = await ApplyAsync(job.Gpu, job.Watts,
                () => authority.ConsentVersion == lease.ConsentVersion && lease.Active,
                start => authority.TryBegin(lease, start), token,
                () => LastAttempt = new("unknown", null, false)).ConfigureAwait(false);
            LastAttempt = new("unknown", measured.ObservedWatts, false);
            using var receipt = await exchange(new
            {
                id = job.Id, phase = measured.Phase, observed_watts = measured.ObservedWatts,
            }, token).ConfigureAwait(false);
            if (IsOk(receipt.RootElement)) LastAttempt = measured with { Confirmed = true };
        }
        catch (Exception error) when (IsUnconfirmed(error, token)) { }
    }

    private static bool TryClaimLease(JsonElement value, out long lease)
    {
        lease = 0;
        return value.ValueKind == JsonValueKind.Object && value.EnumerateObject().Count() == 2
            && value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True
            && value.TryGetProperty("lease_expires", out var until) && until.ValueKind == JsonValueKind.Number
            && until.TryGetInt64(out lease) && lease is >= 1 and <= 253402300799;
    }

    private async Task<NvidiaPowerAttempt> ApplyAsync(string pci, int watts, Func<bool> authorized,
        Func<Func<bool>, bool> permitStart, CancellationToken token, Action? onStarted = null)
    {
        bool started = false;
        try
        {
            if (token.IsCancellationRequested || !authorized()) return new("refused", null, false);
            var before = await adapter.ReadAsync(pci, token).ConfigureAwait(false);
            if (before is not { IsValid: true }) return new("refused", null, false);
            if (watts < before.Min || watts > before.Max || token.IsCancellationRequested || !authorized())
                return new("refused", before.Current, false);
            if (before.Current == watts) return new("applied", before.Current, false);

            var result = await adapter.WriteAsync(pci, watts, start => permitStart(() =>
            {
                if (token.IsCancellationRequested || !start()) return false;
                started = true;
                onStarted?.Invoke();
                return true;
            }), token).ConfigureAwait(false);
            if (result == NvidiaPowerWrite.NotStarted) return new("refused", before.Current, false);
            var after = await adapter.ReadAsync(pci, token).ConfigureAwait(false);
            double? observed = after is { IsValid: true } ? after.Current : null;
            return new(result == NvidiaPowerWrite.Completed && observed == watts ? "applied" : "unknown", observed, false);
        }
        catch (Exception error) when (error is OperationCanceledException or IOException or InvalidDataException)
        {
            return new(started ? "unknown" : "refused", null, false);
        }
    }

    private static bool IsOk(JsonElement value) =>
        value.ValueKind == JsonValueKind.Object && value.EnumerateObject().Count() == 1
        && value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;

    private static Job? Parse(JsonElement response)
    {
        if (response.ValueKind != JsonValueKind.Object || response.EnumerateObject().Count() != 1
            || !response.TryGetProperty("job", out var job) || job.ValueKind != JsonValueKind.Object
            || job.EnumerateObject().Count() != 4
            || !job.TryGetProperty("id", out var id) || id.ValueKind != JsonValueKind.String
            || id.GetString() is not { } ident || !JobId.IsMatch(ident)
            || !job.TryGetProperty("gpu", out var gpu) || gpu.ValueKind != JsonValueKind.String
            || gpu.GetString() is not { } pci || !IsGpu(pci)
            || !job.TryGetProperty("watts", out var watts) || watts.ValueKind != JsonValueKind.Number
            || !watts.TryGetInt32(out int requested) || requested is < 1 or > 3000
            || !job.TryGetProperty("expires", out var expires) || expires.ValueKind != JsonValueKind.Number
            || !expires.TryGetInt64(out long end) || end is < 1 or > 253402300799)
            return null;
        return new(ident, pci, requested, end);
    }

    private static bool IsUnconfirmed(Exception error, CancellationToken token) =>
        error is HttpRequestException { StatusCode: not HttpStatusCode.Unauthorized } or IOException or InvalidDataException
        || error is OperationCanceledException && !token.IsCancellationRequested;

    private sealed record Job(string Id, string Gpu, int Watts, long Expires);
}
