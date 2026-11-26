using System.Diagnostics.CodeAnalysis;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Apollo.Neb.Desktop;

internal enum FleetStage { Pending, Verified, Rejected }

internal sealed record FleetIdentity(string Origin, string Id, string Key, FleetStage Stage)
{
    public override string ToString() => nameof(FleetIdentity);
}

internal sealed class FleetLink : IDisposable
{
    private const int PayloadLimit = 64 * 1024;
    private const int DeviceLimit = 32;
    private const long MaxMemoryMiB = 1L << 24;
    private const long MaxUnixSeconds = 253402300799;
    private static readonly Regex Invitation = new(@"\A([0-9a-f]{24})\.([A-Za-z0-9_-]{24})\z");
    private static readonly Regex NodeId = new(@"\A[0-9a-f]{24}\z");
    private static readonly Regex NodeKey = new(@"\A[A-Za-z0-9_-]{43}\z");
    private static readonly Regex Release = new(@"\A(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}(?:-rc\.(?:0|[1-9][0-9]{0,3}))?\z");

    private readonly FleetCredentialStore _store;
    private readonly HttpClient _client;
    private readonly string _version;
    private HardwareSchema _hardwareSchema;

    internal FleetLink(FleetCredentialStore store, string version, HttpMessageHandler? handler = null)
    {
        if (!Release.IsMatch(version)) throw new ArgumentException("Invalid Desktop version.", nameof(version));
        _store = store;
        _version = version;
        _client = new HttpClient(handler ?? new HttpClientHandler
        {
            AllowAutoRedirect = false,
            UseProxy = false,
            UseCookies = false,
            UseDefaultCredentials = false,
            MaxResponseHeadersLength = 16,
        });
    }

    internal FleetIdentity? Current { get; private set; }

    internal static bool TryOrigin(string value, [NotNullWhen(true)] out Uri? origin)
    {
        origin = null;
        string text = value.Trim();
        if (text.Length > 300 || !text.StartsWith("https://", StringComparison.OrdinalIgnoreCase)
            || text.Any(c => char.IsWhiteSpace(c) || char.IsControl(c) || c is '\\' or '?' or '#' or '@' or '%'))
            return false;
        ReadOnlySpan<char> authority = text.AsSpan(8);
        int slash = authority.IndexOf('/');
        if (slash >= 0 && slash != authority.Length - 1) return false;
        if (slash >= 0) authority = authority[..slash];
        if (authority.IsEmpty || authority[^1] == ':'
            || !Uri.TryCreate(text, UriKind.Absolute, out var candidate) || candidate.Port is < 1 or > 65535
            || candidate.HostNameType is UriHostNameType.Unknown or UriHostNameType.Basic
            || candidate.AbsolutePath != "/" || candidate.UserInfo.Length != 0)
            return false;
        origin = new Uri(candidate.GetLeftPart(UriPartial.Authority) + "/");
        return true;
    }

    internal static bool IsInvitation(string value) => Invitation.IsMatch(value.Trim());

    internal static bool IsStoredIdentity(FleetIdentity identity) =>
        identity.Origin is string address && TryOrigin(address, out var origin) && address == origin.AbsoluteUri
        && identity.Id is string id && NodeId.IsMatch(id)
        && identity.Key is string key && (identity.Stage switch
        {
            FleetStage.Pending or FleetStage.Verified => NodeKey.IsMatch(key),
            FleetStage.Rejected => key.Length == 0,
            _ => false,
        });

    internal async Task LoadAsync(CancellationToken token) =>
        Current = await _store.LoadAsync(reserve: false, token).ConfigureAwait(false);

    internal async Task<long> PairAsync(Uri origin, string invitation, IReadOnlyList<NvidiaGpuReading> devices,
        CancellationToken token)
    {
        if (!origin.IsAbsoluteUri || !TryOrigin(origin.OriginalString, out var destination))
            throw new ArgumentException("A bare HTTPS origin is required.");
        Match parsed = Invitation.Match(invitation.Trim());
        if (!parsed.Success) throw new ArgumentException("Invalid composite invitation.");
        string id = parsed.Groups[1].Value;
        string code = parsed.Groups[2].Value;
        Current ??= await _store.LoadAsync(reserve: true, token).ConfigureAwait(false);
        var identity = Current;
        bool retrying = identity is not null;
        if (identity is not null)
        {
            if (identity.Stage != FleetStage.Pending)
                throw new InvalidOperationException("Disconnect the existing link first.");
            if (identity.Origin != destination.AbsoluteUri || identity.Id != id)
                throw new ArgumentException("The pending link belongs to another panel or node.");
        }
        else
        {
            byte[] random = RandomNumberGenerator.GetBytes(32);
            string key = Convert.ToBase64String(random).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            CryptographicOperations.ZeroMemory(random);
            identity = new FleetIdentity(destination.AbsoluteUri, id, key, FleetStage.Pending);
            await _store.SaveAsync(identity, token).ConfigureAwait(false);
            Current = identity;
            _hardwareSchema = HardwareSchema.FactoryDefault;
        }

        try
        {
            using var reply = await PostAsync(identity, "pair", new { id, code, key = identity.Key }, token)
                .ConfigureAwait(false);
            if (!HasFields(reply.RootElement, 1) || !HasOk(reply.RootElement)) throw InvalidReply();
        }
        catch (HttpRequestException error) when (retrying && error.StatusCode == HttpStatusCode.Unauthorized)
        {
            // The invite may expire after a pair response was lost. Prove possession of the same node key.
            await CheckAsync(identity, token).ConfigureAwait(false);
        }

        await PollAsync(identity, devices, token).ConfigureAwait(false);
        long seen = await CheckAsync(identity, token).ConfigureAwait(false) ?? throw InvalidReply();
        var verified = identity with { Stage = FleetStage.Verified };
        await _store.SaveAsync(verified, token).ConfigureAwait(false);
        Current = verified;
        return seen;
    }

    internal async Task<long> ReportAsync(IReadOnlyList<NvidiaGpuReading> devices, bool check, CancellationToken token,
        NvidiaPowerControl? power = null)
    {
        var identity = Current is { Stage: FleetStage.Verified } active
            ? active : throw new InvalidOperationException("An active link is required.");
        try
        {
            long? consentVersion = power?.OfferedConsent;
            var advertised = await PollAsync(identity, devices, token, consentVersion is not null).ConfigureAwait(false);
            if (power is not null && consentVersion is long offered && advertised.Count > 0)
                await power.RunAsync(advertised, offered, (receipt, stop) =>
                    PostAsync(identity, "power", new { id = identity.Id, key = identity.Key, receipt }, stop), token)
                    .ConfigureAwait(false);
            return check ? await CheckAsync(identity, token).ConfigureAwait(false) ?? throw InvalidReply()
                : DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        }
        catch (HttpRequestException error) when (error.StatusCode == HttpStatusCode.Unauthorized)
        {
            var rejected = identity with { Key = "", Stage = FleetStage.Rejected };
            Current = rejected;
            await _store.SaveAsync(rejected, token).ConfigureAwait(false);
            throw;
        }
    }

    internal void Forget()
    {
        _store.Forget();
        Current = null;
        _hardwareSchema = HardwareSchema.FactoryDefault;
    }

    private async Task<IReadOnlySet<string>> PollAsync(FleetIdentity identity, IReadOnlyList<NvidiaGpuReading> devices,
        CancellationToken token, bool allowPower = false)
    {
        var projected = devices.DistinctBy(device => NvidiaTelemetry.CanonicalAddress(device.BusId)).Take(DeviceLimit)
            .Select(device =>
            {
                long? total = device.MemoryTotalMiB is >= 1 and <= MaxMemoryMiB ? device.MemoryTotalMiB : null;
                long? used = device.MemoryUsedMiB is >= 0 and <= MaxMemoryMiB ? device.MemoryUsedMiB : null;
                return new FleetDevice(NvidiaTelemetry.CanonicalAddress(device.BusId), HardwareLabel(device.Name),
                    "nvidia", Sensor(device.Temperature, 160), Sensor(device.Watts, 3000),
                    Sensor(device.Utilization, 100), used > total ? null : used, total,
                    device.PowerLimit is { IsValid: true } limits ? limits : null);
            }).ToArray();
        var schema = _hardwareSchema;
        while (true)
        {
            var rows = schema switch
            {
                HardwareSchema.Telemetry => projected.Select(device => device with { power_limit = null }).ToArray(),
                HardwareSchema.Bounds => projected.Select(device => device.power_limit is { DefaultWatts: not null } limits
                    ? device with { power_limit = limits with { DefaultWatts = null } } : device).ToArray(),
                _ => projected,
            };
            var supported = rows.Where(device => device.power_limit is not null).Select(device => device.id).ToHashSet();
            bool advertise = allowPower && supported.Count > 0;
            try
            {
                await SendPollAsync(identity, new FleetHardware(rows, advertise ? true : null), token).ConfigureAwait(false);
                _hardwareSchema = schema;
                return advertise ? supported : new HashSet<string>();
            }
            catch (HttpRequestException error) when (supported.Count > 0 &&
                error.StatusCode is HttpStatusCode.BadRequest or HttpStatusCode.Conflict)
            {
                // Older panels may accept the original power bounds before falling back to telemetry alone.
                schema = rows.Any(device => device.power_limit?.DefaultWatts is not null)
                    ? HardwareSchema.Bounds : HardwareSchema.Telemetry;
            }
        }
    }

    private async Task SendPollAsync(FleetIdentity identity, FleetHardware hardware, CancellationToken token)
    {
        using var reply = await PostAsync(identity, "poll", new
        {
            id = identity.Id,
            key = identity.Key,
            workers = Array.Empty<object>(),
            acks = Array.Empty<object>(),
            release = new { version = _version, platform = "windows", arch = "x64", kernel = (string?)null },
            hardware,
        }, token).ConfigureAwait(false);
        // The Windows GPU monitor does not process miner commands.
        if (!HasFields(reply.RootElement, 1) || !reply.RootElement.TryGetProperty("commands", out var commands)
            || commands.ValueKind != JsonValueKind.Array || commands.GetArrayLength() > 16)
            throw InvalidReply();
    }

    private enum HardwareSchema { FactoryDefault, Bounds, Telemetry }

    private sealed record FleetDevice(string id, string label, string vendor, double? edge, double? watts,
        double? utilization, long? memory_used_mib, long? memory_total_mib,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] NvidiaPowerLimit? power_limit);
    private sealed record FleetHardware(FleetDevice[] devices,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? power_control);

    private async Task<long?> CheckAsync(FleetIdentity identity, CancellationToken token)
    {
        using var reply = await PostAsync(identity, "check", new { id = identity.Id, key = identity.Key }, token)
            .ConfigureAwait(false);
        var response = reply.RootElement;
        if (!HasFields(response, 2) || !HasOk(response) || !response.TryGetProperty("last_seen", out var seen))
            throw InvalidReply();
        if (seen.ValueKind == JsonValueKind.Null) return null;
        if (seen.ValueKind != JsonValueKind.Number || !seen.TryGetInt64(out long seconds) || seconds is < 1 or > MaxUnixSeconds)
            throw InvalidReply();
        return seconds;
    }

    private async Task<JsonDocument> PostAsync<T>(FleetIdentity identity, string action, T value, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(5));
        byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(value);
        try
        {
            if (bytes.Length > PayloadLimit) throw new InvalidDataException("Fleet request exceeds the size limit.");
            using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(new Uri(identity.Origin), "/api/node/" + action));
            request.Content = new ByteArrayContent(bytes);
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            using var response = await _client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
                deadline.Token).ConfigureAwait(false);
            if (response.StatusCode != HttpStatusCode.OK)
                throw new HttpRequestException("Fleet rejected the request.", null, response.StatusCode);
            if (!string.Equals(response.Content.Headers.ContentType?.MediaType, "application/json",
                    StringComparison.OrdinalIgnoreCase) || response.Content.Headers.ContentLength > PayloadLimit)
                throw InvalidReply();

            using var body = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
            using var buffer = new MemoryStream();
            byte[] block = new byte[4096];
            int read;
            while ((read = await body.ReadAsync(block, deadline.Token).ConfigureAwait(false)) != 0)
            {
                if (buffer.Length + read > PayloadLimit) throw InvalidReply();
                buffer.Write(block, 0, read);
            }
            buffer.Position = 0;
            return await JsonDocument.ParseAsync(buffer, new JsonDocumentOptions { MaxDepth = 12 }, deadline.Token)
                .ConfigureAwait(false);
        }
        catch (JsonException) { throw InvalidReply(); }
        finally { CryptographicOperations.ZeroMemory(bytes); }
    }

    private static bool HasFields(JsonElement value, int count) =>
        value.ValueKind == JsonValueKind.Object && value.EnumerateObject().Count() == count;

    private static bool HasOk(JsonElement value) =>
        value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;

    private static InvalidDataException InvalidReply() => new("Unconfirmed Fleet reply.");

    private static string HardwareLabel(string name)
    {
        string label = new string(name.Select(c => c is >= ' ' and <= '~' ? c : ' ').Take(80).ToArray()).Trim();
        return label.Length > 0 ? label : "NVIDIA";
    }

    private static double? Sensor(double? value, double maximum) =>
        value is double number && number >= 0 && number <= maximum ? value : null;

    public void Dispose()
    {
        _client.Dispose();
        _store.Dispose();
    }
}
