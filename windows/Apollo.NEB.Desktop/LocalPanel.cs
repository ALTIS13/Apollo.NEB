using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;

namespace Apollo.Neb.Desktop;

internal sealed record PublicStatus(DateTimeOffset ObservedAt, int Total, int Running, int Attention,
    double? Hashrate, double? Watts, double? Edge, double? Hotspot, double? Memory,
    long? Accepted, long? Rejected, bool FeeEnabled, int FeeBasisPoints, string FeeState);

internal sealed class LocalPanel : IDisposable
{
    internal const int DefaultPort = 18763;
    private const int MaxPayload = 64 * 1024;
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(4);

    private readonly HttpClient _client = new(new HttpClientHandler
    {
        AllowAutoRedirect = false,
        UseProxy = false,
        UseCookies = false,
        UseDefaultCredentials = false,
    });

    internal static bool TryPort(string value, out int port) =>
        int.TryParse(value.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out port)
        && port is >= 1 and <= 65535;

    internal static Uri HtmlUri(int port) => Endpoint(port, "/");

    private static Uri Endpoint(int port, string path)
    {
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        return new UriBuilder(Uri.UriSchemeHttp, "127.0.0.1", port, path).Uri;
    }

    internal async Task<PublicStatus> ReadAsync(int port, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(Timeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, Endpoint(port, "/api/public/status"));
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        using var response = await _client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            deadline.Token).ConfigureAwait(false);
        if (response.StatusCode != HttpStatusCode.OK)
            throw new HttpRequestException("Unexpected local HTTP status.", null, response.StatusCode);
        if (!string.Equals(response.Content.Headers.ContentType?.MediaType, "application/json",
                StringComparison.OrdinalIgnoreCase) || response.Content.Headers.ContentLength > MaxPayload)
            throw new InvalidDataException("Invalid local snapshot response.");

        using var content = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
        using var payload = new MemoryStream();
        byte[] buffer = new byte[4096];
        int length;
        while ((length = await content.ReadAsync(buffer, deadline.Token).ConfigureAwait(false)) > 0)
        {
            if (payload.Length + length > MaxPayload)
                throw new InvalidDataException("Local snapshot exceeds the response limit.");
            payload.Write(buffer, 0, length);
        }

        payload.Position = 0;
        try
        {
            using var data = await JsonDocument.ParseAsync(payload, cancellationToken: deadline.Token)
                .ConfigureAwait(false);
            return Parse(data.RootElement);
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or
                                      KeyNotFoundException or FormatException or ArgumentOutOfRangeException)
        {
            throw new InvalidDataException("Invalid local snapshot.", error);
        }
    }

    private static PublicStatus Parse(JsonElement json)
    {
        var workers = json.GetProperty("workers");
        var temperatures = json.GetProperty("temperatures");
        var shares = json.GetProperty("shares");
        var fee = json.GetProperty("devfee");
        var total = workers.GetProperty("total").GetInt32();
        var running = workers.GetProperty("running").GetInt32();
        var attention = workers.GetProperty("attention").GetInt32();
        var timestamp = DateTimeOffset.FromUnixTimeSeconds(json.GetProperty("time").GetInt64());
        var basisPoints = fee.GetProperty("basis_points").GetInt32();
        var state = fee.GetProperty("state").GetString();

        if (total < 0 || running < 0 || attention < 0 || (long)running + attention > total
            || timestamp <= DateTimeOffset.UnixEpoch || timestamp > DateTimeOffset.UtcNow.AddSeconds(5)
            || basisPoints is < 0 or > 10_000 || state is null or { Length: 0 or > 32 }
            || state.Any(value => value is not (>= 'a' and <= 'z' or '-' or '_')))
            throw new InvalidDataException("Inconsistent local snapshot.");

        return new PublicStatus(timestamp, total, running, attention,
            Measurement(json, "hashrate_hs", 0, double.MaxValue), Measurement(json, "watts", 0, double.MaxValue),
            Measurement(temperatures, "edge", -100, 300), Measurement(temperatures, "hot", -100, 300),
            Measurement(temperatures, "hbm", -100, 300),
            Shares(shares, "accepted"), Shares(shares, "rejected"),
            fee.GetProperty("enabled").GetBoolean(), basisPoints, state);
    }

    private static double? Measurement(JsonElement values, string property, double minimum, double maximum)
    {
        var value = values.GetProperty(property);
        if (value.ValueKind == JsonValueKind.Null) return null;
        if (!value.TryGetDouble(out var number) || !double.IsFinite(number) || number < minimum || number > maximum)
            throw new InvalidDataException("Invalid measurement.");
        return number;
    }

    private static long? Shares(JsonElement values, string property)
    {
        var value = values.GetProperty(property);
        if (value.ValueKind == JsonValueKind.Null) return null;
        if (!value.TryGetInt64(out var number) || number < 0)
            throw new InvalidDataException("Invalid share count.");
        return number;
    }

    public void Dispose() => _client.Dispose();
}
