using System.Net;
using System.Net.Sockets;
using System.Text;
using Apollo.Neb.Desktop;

using var timeLimit = new CancellationTokenSource(TimeSpan.FromSeconds(12));
using var panel = new LocalPanel();
var cancel = timeLimit.Token;
string sample = $$"""
    {"time":{{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}},
      "workers":{"total":3,"running":2,"attention":1},"hashrate_hs":2468000,"watts":null,
      "temperatures":{"edge":54,"hot":68,"hbm":null},"shares":{"accepted":12,"rejected":0},
      "devfee":{"enabled":true,"basis_points":110,"state":"counting"},"wallet":"synthetic-field"}
    """;

using var site = new TcpListener(IPAddress.Loopback, 0);
site.Start();
int port = ((IPEndPoint)site.LocalEndpoint).Port;
var validReply = RespondAsync(site, 200, sample, cancel);
PublicStatus? observation = await panel.ReadAsync(port, cancel);
Check(observation is { Total: 3, Running: 2, Attention: 1, Hashrate: 2468000, Watts: null,
    Edge: 54, Hotspot: 68, Memory: null, Accepted: 12, Rejected: 0, FeeBasisPoints: 110, FeeState: "counting" },
    "A live snapshot preserves real measurements and unknown sensors.");
Check(await validReply == "GET /api/public/status HTTP/1.1", "Use the public snapshot route.");

var redirect = RespondAsync(site, 302, "", cancel, $"Location: http://127.0.0.1:{port}/elsewhere\r\n");
await RejectsAsync<HttpRequestException>(() => panel.ReadAsync(port, cancel));
await redirect;
Check(!site.Pending(), "Never follow a redirect to another resource.");

var oversized = RespondAsync(site, 200, new string(' ', 65_537), cancel);
await RejectsAsync<InvalidDataException>(() => panel.ReadAsync(port, cancel));
await oversized;

var inconsistent = RespondAsync(site, 200, sample.Replace("\"running\":2", "\"running\":4"), cancel);
await RejectsAsync<InvalidDataException>(() => panel.ReadAsync(port, cancel));
await inconsistent;

Check(!LocalPanel.TryPort("https://other.example", out _) && !LocalPanel.TryPort("65536", out _)
    && LocalPanel.TryPort("18763", out int validPort) && validPort == 18763,
    "A numeric loopback port is the only editable source.");
Check(LocalPanel.HtmlUri(18763).AbsoluteUri == "http://127.0.0.1:18763/", "Open the local panel.");
var devices = NvidiaTelemetry.Parse(""""
    "NVIDIA, RTX ""fixture""", 00000000:02:00.0, 55, 45, 4096, 12288, 144.50
    Second GPU, 0000:03:00.0, [N/A], 0, N/A, 8192, [Not Supported]
    """");
Check(devices.Count == 2, "The CSV reader keeps every GPU.");
Check(devices[0] is { Name: "NVIDIA, RTX \"fixture\"", BusId: "00000000:02:00.0", Temperature: 55,
    Utilization: 45, MemoryUsedMiB: 4096, MemoryTotalMiB: 12288, Watts: 144.5 },
    "A real CSV row can include a quoted name and decimal measurements.");
Check(devices[1] is { Name: "Second GPU", BusId: "0000:03:00.0", Temperature: null, Utilization: 0,
    MemoryUsedMiB: null, MemoryTotalMiB: 8192, Watts: null },
    "Unavailable NVIDIA measurements stay null and an actual zero stays zero.");
Check(NvidiaTelemetry.Parse("").Count == 0, "No GPU rows remains an empty list.");
try
{
    NvidiaTelemetry.Parse("Bad fixture, 0000:04:00.0, 50, 101, 400, 1000, 5.2");
    throw new InvalidOperationException("An out-of-range utilization was accepted.");
}
catch (InvalidDataException) { }

Console.WriteLine("PASS: loopback GET, typed metrics, nulls, invalid counts, response limit, and redirect refusal, and NVIDIA CSV.");

await FleetLinkSmoke.RunAsync(cancel);
await NvidiaPowerSmoke.RunAsync(cancel);

if (args is ["--query-installed-nvidia"])
{
    var installed = await NvidiaTelemetry.ReadAsync(cancel);
    Check(installed.State == NvidiaQueryState.Ready, $"Installed query returned {installed.State}.");
    Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(installed.Devices));
}

if (args is ["--read-installed-nvidia-power"])
{
    var snapshot = await NvidiaTelemetry.ReadAsync(cancel);
    var readOnly = new NvidiaTelemetry.PowerAdapter();
    Check(snapshot.State == NvidiaQueryState.Ready && readOnly.Available,
        "The signed installed NVIDIA tool is unavailable.");
    foreach (var gpu in snapshot.Devices.Where(device => device.PowerLimit is not null))
    {
        var limit = await readOnly.ReadAsync(NvidiaTelemetry.CanonicalAddress(gpu.BusId), cancel);
        Check(limit is { IsValid: true }, "The signed tool did not confirm power limits for the same PCI device.");
        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(limit));
    }
}


static void Check(bool condition, string purpose)
{
    if (!condition) throw new InvalidOperationException(purpose);
}

static async Task RejectsAsync<TException>(Func<Task> attempt) where TException : Exception
{
    try
    {
        await attempt();
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException($"Expected {typeof(TException).Name}.");
}

static async Task<string?> RespondAsync(TcpListener server, int status, string json,
    CancellationToken token, string headers = "")
{
    using TcpClient remote = await server.AcceptTcpClientAsync(token);
    using NetworkStream stream = remote.GetStream();
    using var reader = new StreamReader(stream, Encoding.ASCII, leaveOpen: true);
    string? request = await reader.ReadLineAsync(token);
    string? line;
    do
    {
        line = await reader.ReadLineAsync(token);
        Check(line is not null &&
            !line.StartsWith("Cookie:", StringComparison.OrdinalIgnoreCase) &&
            !line.StartsWith("Authorization:", StringComparison.OrdinalIgnoreCase),
            "Do not transmit credentials.");
    }
    while (line!.Length > 0);

    byte[] body = Encoding.UTF8.GetBytes(json);
    byte[] head = Encoding.ASCII.GetBytes($"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
        + $"Content-Length: {body.Length}\r\nConnection: close\r\n{headers}\r\n");
    await stream.WriteAsync(head, token);
    await stream.WriteAsync(body, token);
    return request;
}
