using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Apollo.Neb.Desktop;

internal static class FleetLinkSmoke
{
    private const string Node = "00112233445566778899aabb";
    private const string Code = "abcdefghijklmnopqrstuvwx";
    private const string Version = "0.9.1-rc.1";

    internal static async Task RunAsync(CancellationToken token)
    {
        Require(FleetLink.TryOrigin("https://test.example:8443/", out var origin), "Accept a bare HTTPS panel origin.");
        foreach (string input in new[] { "http://test.example", "https://test.example/x", "https://test.example/../",
            @"https://test.example\path", "https://test.example?#", "https://test.example?", "https://@test.example",
            "https://test.example/%2e", "https://test.example:", "https://test.example:0" })
            Require(!FleetLink.TryOrigin(input, out _), "Reject ambiguous or non-HTTPS origins before a request.");

        string directory = Path.Combine(Path.GetTempPath(), "apollo-neb-link-" + Guid.NewGuid().ToString("N"));
        string path = Path.Combine(directory, "fleet.dat");
        var panel = new Fixture(origin!);
        var gpus = NvidiaTelemetry.Parse("Fixture NVIDIA, 00000000:0A:00.0, 55, 0, N/A, 8192, [Not Supported]");
        try
        {
            using (var first = new FleetLink(new FleetCredentialStore(path), Version, panel.Transport()))
            {
                await first.LoadAsync(token);
                Require(first.Current is null && panel.Routes.Count == 0 && !Directory.Exists(directory),
                    "A new install neither creates remote state nor sends traffic.");
                await ThrowsAsync<ArgumentException>(() => first.PairAsync(origin!, Node + ".short", gpus, token));
                Require(first.Current is null && panel.Routes.Count == 0 && !File.Exists(path),
                    "Reject a malformed invitation before storage and network.");
                panel.LoseNextPairReply = true;
                await ThrowsAsync<HttpRequestException>(() => first.PairAsync(origin!, Node + "." + Code, gpus, token));
                Require(first.Current is { Stage: FleetStage.Pending } && panel.Routes.SequenceEqual(["pair"]),
                    "An unknown pairing result stays pending and triggers no telemetry.");
                byte[] saved = File.ReadAllBytes(path);
                Require(saved.AsSpan().IndexOf(Encoding.UTF8.GetBytes(panel.ServerKey!)) < 0 &&
                    saved.AsSpan().IndexOf(Encoding.UTF8.GetBytes(Code)) < 0,
                    "The retry key is protected and the one-time code is absent from disk.");
            }

            using (var retry = new FleetLink(new FleetCredentialStore(path), Version, panel.Transport()))
            {
                int before = panel.Routes.Count;
                await retry.LoadAsync(token);
                Require(retry.Current is { Stage: FleetStage.Pending } && panel.Routes.Count == before,
                    "Pending is restored after restart without connecting.");
                Require(FleetLink.TryOrigin("https://other.example/", out var other), "Set up a second panel origin.");
                await ThrowsAsync<ArgumentException>(() => retry.PairAsync(other!, Node + "." + Code, gpus, token));
                Require(panel.Routes.Count == before, "Never retry a pending key against a different origin.");

                panel.InviteExpired = true;
                panel.InvalidNextCheck = 2;
                await ThrowsAsync<InvalidDataException>(() => retry.PairAsync(origin!, Node + "." + Code, gpus, token));
                Require(retry.Current is { Stage: FleetStage.Pending }, "A poll without a valid final check cannot be active.");
                Require(panel.Routes.Skip(before).SequenceEqual(["pair", "check", "poll", "check"]),
                    "A lost reply recovers with the same identity even when the invitation has expired.");
                long when = await retry.PairAsync(origin!, Node + "." + Code, gpus, token);
                Require(when > 0 && retry.Current is { Stage: FleetStage.Verified }, "Activate only after poll and check.");

                panel.RedirectNextPoll = true;
                await ThrowsAsync<HttpRequestException>(() => retry.ReportAsync(gpus, false, token));
                Require(retry.Current is { Stage: FleetStage.Verified }, "A redirect is an error, not a verified sample.");
                panel.OversizeNextPoll = true;
                await ThrowsAsync<InvalidDataException>(() => retry.ReportAsync(gpus, false, token));
                panel.Revoked = true;
                await ThrowsAsync<HttpRequestException>(() => retry.ReportAsync(gpus, false, token));
                Require(retry.Current is { Stage: FleetStage.Rejected }, "An owner-rejected key stops automatic telemetry.");
            }
            using var reopened = new FleetLink(new FleetCredentialStore(path), Version, panel.Transport());
            await reopened.LoadAsync(token);
            int requests = panel.Routes.Count;
            Require(reopened.Current is { Stage: FleetStage.Rejected }, "Rejected state persists across restarts.");
            await ThrowsAsync<InvalidOperationException>(() => reopened.ReportAsync(gpus, false, token));
            Require(panel.Routes.Count == requests, "A rejected key never sends another request.");
            reopened.Forget();
            Require(reopened.Current is null && !File.Exists(path), "Local disconnect removes credentials.");
            await RejectUntrustedTlsAsync(path, token);
        }
        finally
        {
            if (Directory.Exists(directory))
            {
                File.Delete(path);
                File.Delete(path + ".lock");
                Directory.Delete(directory);
            }
        }
        Console.WriteLine("PASS: explicit Windows Fleet enrollment, DPAPI pending retry, poll + check, exact DTO, revocation, and refusal of an untrusted TLS certificate.");
    }

    private static async Task RejectUntrustedTlsAsync(string path, CancellationToken token)
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        using var secret = RSA.Create(2048);
        var request = new CertificateRequest("CN=neb-local-fixture", secret, HashAlgorithmName.SHA256,
            RSASignaturePadding.Pkcs1);
        var names = new SubjectAlternativeNameBuilder();
        names.AddIpAddress(IPAddress.Loopback);
        request.CertificateExtensions.Add(names.Build());
        using var certificate = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1),
            DateTimeOffset.UtcNow.AddMinutes(2));

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(8));
        Task<bool> received = CanReadRequestAsync(listener, certificate, deadline.Token);
        using var client = new FleetLink(new FleetCredentialStore(path), Version);
        try
        {
            await client.PairAsync(new Uri($"https://127.0.0.1:{port}/"), Node + "." + Code, [], deadline.Token);
            throw new InvalidOperationException("The production client accepted an untrusted TLS chain.");
        }
        catch (HttpRequestException error) when (error.HttpRequestError == HttpRequestError.SecureConnectionError) { }
        Require(!await received, "An untrusted TLS connection receives no node data.");
    }

    private static async Task<bool> CanReadRequestAsync(TcpListener listener, X509Certificate2 certificate,
        CancellationToken token)
    {
        using var connection = await listener.AcceptTcpClientAsync(token);
        using var tls = new SslStream(connection.GetStream());
        try
        {
            await tls.AuthenticateAsServerAsync(new SslServerAuthenticationOptions { ServerCertificate = certificate }, token);
            return await tls.ReadAsync(new byte[1], token) != 0;
        }
        catch (Exception error) when (error is AuthenticationException or IOException) { return false; }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static async Task ThrowsAsync<TException>(Func<Task> attempt) where TException : Exception
    {
        try { await attempt(); }
        catch (TException) { return; }
        throw new InvalidOperationException($"Expected {typeof(TException).Name}.");
    }

    private sealed class Fixture(Uri origin)
    {
        internal List<string> Routes { get; } = [];
        internal string? ServerKey { get; private set; }
        internal bool LoseNextPairReply { get; set; }
        internal bool InviteExpired { get; set; }
        internal int InvalidNextCheck { get; set; }
        internal bool RedirectNextPoll { get; set; }
        internal bool OversizeNextPoll { get; set; }
        internal bool Revoked { get; set; }
        private long? _lastSeen;

        internal HttpMessageHandler Transport() => new Handler(this);

        private sealed class Handler(Fixture fixture) : HttpMessageHandler
        {
            protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
            {
                using var data = JsonDocument.Parse(await request.Content!.ReadAsByteArrayAsync(token));
                return fixture.Receive(request, data.RootElement);
            }
        }

        private HttpResponseMessage Receive(HttpRequestMessage request, JsonElement body)
        {
            Uri destination = request.RequestUri!;
            Require(destination.GetLeftPart(UriPartial.Authority) == origin.GetLeftPart(UriPartial.Authority)
                && destination.Query == "" && destination.Fragment == "" && request.Method == HttpMethod.Post
                && request.Content?.Headers.ContentType?.MediaType == "application/json"
                && !request.Headers.Contains("Cookie") && !request.Headers.Contains("Authorization")
                && !request.Headers.Contains("Origin"), "Use only the chosen HTTPS origin and node contract.");
            string route = destination.AbsolutePath.Split('/')[^1];
            Require(destination.AbsolutePath == "/api/node/" + route, "Use the exact node paths.");
            Routes.Add(route);
            Require(body.GetProperty("id").GetString() == Node, "The request identifies the selected node.");
            string? key = body.GetProperty("key").GetString();
            Require(key is not null && Regex.IsMatch(key, @"\A[A-Za-z0-9_-]{43}\z"), "Use a base64url 32-byte node key.");
            Require(ServerKey is null || ServerKey == key, "Reuse the persisted key after a lost reply.");
            switch (route)
            {
                case "pair":
                    Exact(body, "id", "code", "key");
                    Require(body.GetProperty("code").GetString() == Code, "Only pair receives the invitation code.");
                    if (InviteExpired) return Reply(HttpStatusCode.Unauthorized, "{}");
                    ServerKey = key;
                    if (LoseNextPairReply)
                    {
                        LoseNextPairReply = false;
                        throw new HttpRequestException("Fixture lost the server reply after pairing.");
                    }
                    return Reply(HttpStatusCode.OK, """{"ok":true}""");
                case "check":
                    Exact(body, "id", "key");
                    if (InvalidNextCheck > 0 && --InvalidNextCheck == 0) return Reply(HttpStatusCode.OK, """{"ok":false}""");
                    return Reply(HttpStatusCode.OK, JsonSerializer.Serialize(new { ok = true, last_seen = _lastSeen }));
                case "poll":
                    Exact(body, "id", "key", "workers", "acks", "release", "hardware");
                    Require(body.GetProperty("workers").GetArrayLength() == 0 && body.GetProperty("acks").GetArrayLength() == 0,
                        "Hardware must stay separate from mining and command acknowledgements.");
                    var release = body.GetProperty("release");
                    Exact(release, "version", "platform", "arch", "kernel");
                    Require(release.GetProperty("version").GetString() == Version &&
                        release.GetProperty("platform").GetString() == "windows" && release.GetProperty("arch").GetString() == "x64"
                        && release.GetProperty("kernel").ValueKind == JsonValueKind.Null, "Send the exact Windows release identity.");
                    var hardware = body.GetProperty("hardware");
                    Exact(hardware, "devices");
                    var devices = hardware.GetProperty("devices");
                    Require(devices.GetArrayLength() == 1, "Send the observed GPU exactly once.");
                    var gpu = devices[0];
                    Exact(gpu, "id", "label", "vendor", "edge", "watts", "utilization", "memory_used_mib", "memory_total_mib");
                    Require(gpu.GetProperty("id").GetString() == "00000000:0a:00.0"
                        && gpu.GetProperty("label").GetString() == "Fixture NVIDIA" && gpu.GetProperty("vendor").GetString() == "nvidia"
                        && gpu.GetProperty("edge").GetInt32() == 55 && gpu.GetProperty("utilization").GetInt32() == 0
                        && gpu.GetProperty("watts").ValueKind == JsonValueKind.Null
                        && gpu.GetProperty("memory_used_mib").ValueKind == JsonValueKind.Null
                        && gpu.GetProperty("memory_total_mib").GetInt32() == 8192, "Preserve PCI, metrics, zero and unavailable values.");
                    if (RedirectNextPoll)
                    {
                        RedirectNextPoll = false;
                        var response = Reply(HttpStatusCode.TemporaryRedirect, "");
                        response.Headers.Location = new Uri("https://wrong-origin.example/");
                        return response;
                    }
                    if (OversizeNextPoll)
                    {
                        OversizeNextPoll = false;
                        return Reply(HttpStatusCode.OK, new string(' ', 65_537));
                    }
                    if (Revoked) return Reply(HttpStatusCode.Unauthorized, "{}");
                    _lastSeen = 1_797_000_000;
                    // Desktop telemetry ignores even a well-formed command.
                    return Reply(HttpStatusCode.OK, """{"commands":[{"id":"00000000000000000000000000000000","action":"start","workers":["gpu0"],"expires":1797000010}]}""");
                default:
                    throw new InvalidOperationException("Unexpected node request.");
            }
        }

        private static HttpResponseMessage Reply(HttpStatusCode status, string payload) =>
            new(status) { Content = new StringContent(payload, Encoding.UTF8, "application/json") };

        private static void Exact(JsonElement value, params string[] names) =>
            Require(value.EnumerateObject().Select(property => property.Name).Order()
                .SequenceEqual(names.Order()), "Send only whitelisted DTO properties.");
    }
}
