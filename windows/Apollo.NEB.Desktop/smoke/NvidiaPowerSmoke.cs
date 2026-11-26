using System.Net;
using System.Text;
using System.Text.Json;
using Apollo.Neb.Desktop;

internal static class NvidiaPowerSmoke
{
    private const string Gpu = "00000000:01:00.0";
    private const string Node = "00112233445566778899aabb";
    private const string Invite = "abcdefghijklmnopqrstuvwx";

    internal static async Task RunAsync(CancellationToken token)
    {
        var samples = NvidiaTelemetry.ParsePowerLimits("""
            0000:01:00.0, 250.00, 175.00, 300.00, 250.00
            00000000:02:00.0, [N/A], [N/A], [N/A]
            00000000:03:00.0, 260, 275, 290
            00000000:04:00.0, Infinity, 1, 3000
            00000000:05:00.0, 250, 1, 3001
            00000000:06:00.0, 250, 200, 300
            0000:06:00.0, 255, 200, 300
            00000000:07:00.0, 222, 175, 300
            00000000:08:00.0, 223, 175, 300, 350
            """);
        Need(samples.Count == 3 && samples.TryGetValue(Gpu, out var first) &&
            JsonSerializer.SerializeToElement(first).GetProperty("default_watts").GetDouble() == 250,
            "A known factory limit is optional NVIDIA telemetry alongside the three valid active limits.");
        foreach (string id in new[] { "00000000:07:00.0", "00000000:08:00.0" })
            Need(samples[id].IsValid && !JsonSerializer.SerializeToElement(samples[id]).TryGetProperty("default_watts", out _),
                "Older NVIDIA output or an inconsistent factory limit preserves the reported range.");
        var devices = NvidiaTelemetry.Parse("Fixture GPU, 0000:01:00.0, 55, 10, N/A, 8192, 47")
            .Select(gpu => gpu with { PowerLimit = samples[Gpu] }).ToArray();
        var noRights = new NvidiaPowerAuthority(elevated: false);
        noRights.Allow(true);
        Need(!noRights.Granted, "Opt-in never confers Windows administrator rights.");
        await TestLocalAsync(token);
        var authority = new NvidiaPowerAuthority(elevated: true);
        var panel = new Panel();
        var adapter = new Adapter(panel.Calls);
        var control = new NvidiaPowerControl(authority, adapter);
        string directory = Path.Combine(Path.GetTempPath(), "apollo-neb-power-smoke-" + Guid.NewGuid().ToString("N"));
        string path = Path.Combine(directory, "link.dat");
        try
        {
            using (var link = new FleetLink(new FleetCredentialStore(path), "0.9.2-rc.1", panel))
            {
                var origin = new Uri("https://power.invalid/");
                await link.PairAsync(origin, Node + "." + Invite, devices, token);
                Need(panel.ReportedDefault, "Forward the verified factory limit to a capable panel.");
                await link.ReportAsync(devices, false, token, control);
                Need(!panel.Advertised && !panel.Calls.Contains("job") && adapter.Writes == 0,
                    "Power collection and sending cannot arm a fresh session.");
                panel.Calls.Clear();
                authority.Allow(true);
                panel.Job = Job(1, 230);
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Advertised && panel.Calls.SequenceEqual(["poll", "job", "claim", "bounds", "write", "bounds", "applied"])
                    && adapter.Writes == 1 && panel.Observed == 230, "Require server claim and actual readback before applied.");

                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job"]) && adapter.Writes == 1,
                    "Decline a duplicate job without another claim or hardware write.");

                panel.Job = Job(2, 231);
                panel.ClaimReply = """{"ok":false}""";
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim"]) && adapter.Writes == 1,
                    "A false claim confirmation cannot reach the adapter.");

                panel.Job = Job(3, 260);
                panel.ClaimReply = null;
                adapter.Bounds = new(230, 175, 250);
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "bounds", "refused"])
                    && adapter.Writes == 1 && panel.Observed == 230, "Recheck the actual range after the claim.");

                panel.Job = Job(4, 231);
                adapter.SetResult = NvidiaPowerWrite.Unknown;
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "bounds", "write", "bounds", "unknown"])
                    && adapter.Writes == 2 && control.LastAttempt?.Phase == "unknown",
                    "An indeterminate tool result stays unknown even if readback matches.");

                panel.Job = Job(5, 232);
                adapter.SetResult = NvidiaPowerWrite.Completed;
                panel.OnClaim = () => authority.Allow(false);
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "refused"]) && adapter.Writes == 2,
                    "Turning off consent in flight prevents the write.");

                authority.Allow(true);
                panel.OnClaim = null;
                panel.Job = Job(6, 232, gpu: "00000000:01:00.0,--reset");
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job"]) && adapter.Writes == 2,
                    "Never send malformed GPU identity to claim, query, or setter.");

                panel.Job = Job(7, 232, expires: 1);
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job"]) && adapter.Writes == 2,
                    "An expired job cannot be claimed or applied.");

                panel.Job = Job(8, 232);
                panel.LoseTerminal = true;
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "bounds", "write", "bounds", "applied"])
                    && adapter.Writes == 3 && control.LastAttempt is { Phase: "unknown", ObservedWatts: 232 },
                    "A missing final response retains measured evidence and an unconfirmed result.");
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(adapter.Writes == 3 && panel.Calls.SequenceEqual(["poll", "job"]),
                    "A lost terminal receipt cannot replay a physical command.");

                panel.Job = Job(9, 233);
                panel.ClaimLease = 1;
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "refused"]) && adapter.Writes == 3,
                    "A stale execution lease never starts the hardware command.");
                panel.ClaimLease = null;
                panel.Job = Job(10, 234);
                panel.ClaimReply = """{"ok":true}""";
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim"]) && adapter.Writes == 3,
                    "A successful-looking claim without an execution lease is unconfirmed.");
                panel.ClaimReply = null;

                panel.Calls.Clear();
                await link.ReportAsync(devices.Select(gpu => gpu with { PowerLimit = null }).ToArray(),
                    false, token, control);
                Need(panel.Calls.SequenceEqual(["poll"]) && !panel.Advertised && adapter.Writes == 3,
                    "An elevated session without current valid bounds cannot publish capability or receive jobs.");

                panel.Job = Job(11, 235);
                panel.OnClaim = () =>
                {
                    authority.Allow(false);
                    authority.Allow(true);
                };
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job", "claim", "refused"]) && adapter.Writes == 3,
                    "A new opt-in cannot revive a claim from the previous permission.");
                panel.OnClaim = null;

                panel.Job = null;
                panel.DefaultUnsupported = true;
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "poll", "job"]) && panel.Advertised && !panel.ReportedDefault &&
                    adapter.Writes == 3, "A server without the factory field still receives remote power capability.");
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "job"]) && panel.Advertised && !panel.ReportedDefault,
                    "Remember the accepted wire schema rather than retrying a rejected field every heartbeat.");

                panel.ExtendedUnsupported = true;
                panel.Calls.Clear();
                await link.ReportAsync(devices, false, token, control);
                Need(panel.Calls.SequenceEqual(["poll", "poll"]) && !panel.Advertised && adapter.Writes == 3,
                    "A legacy panel still receives regular GPU telemetry.");
            }
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
        Console.WriteLine("PASS: NVIDIA factory limits, gated local/remote writes, checked readback, no replay, and legacy telemetry.");
    }

    private static async Task TestLocalAsync(CancellationToken token)
    {
        var calls = new List<string>();
        var adapter = new Adapter(calls);
        var unprivileged = new NvidiaPowerControl(new(elevated: false), adapter);
        Need(!unprivileged.CanApplyLocally &&
            (await unprivileged.ApplyLocalAsync(Gpu, 224, token)).Phase == "refused" && calls.Count == 0,
            "A local write requires an already-elevated process.");

        var authority = new NvidiaPowerAuthority(elevated: true);
        var control = new NvidiaPowerControl(authority, adapter);
        var local = await control.ApplyLocalAsync(Gpu, 225, token);
        Need(control.CanApplyLocally && !authority.Granted && local is { Phase: "applied", ObservedWatts: 225 } &&
            calls.SequenceEqual(["bounds", "write", "bounds"]) && control.LastAttempt is null,
            "Explicit local writes work without a Fleet grant and require fresh bounds and readback.");
        Need((await control.ApplyLocalAsync(Gpu, 301, token)).Phase == "refused" &&
            (await control.ApplyLocalAsync("0000:01:00.0", 226, token)).Phase == "refused" && adapter.Writes == 1,
            "Local limits use exact canonical PCI and the live range.");
        adapter.SetResult = NvidiaPowerWrite.Unknown;
        Need((await control.ApplyLocalAsync(Gpu, 226, token)) is { Phase: "unknown", ObservedWatts: 226 },
            "An unconfirmed local setter stays unknown despite matching readback.");
        adapter.SetResult = NvidiaPowerWrite.Completed;
        authority.Allow(true);
        long consent = authority.ConsentVersion!.Value;
        var advertised = new HashSet<string> { Gpu };

        var writing = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        adapter.OnWriteStarted = stop =>
        {
            writing.SetResult();
            return release.Task.WaitAsync(stop);
        };
        Task<NvidiaPowerAttempt> localWrite = control.ApplyLocalAsync(Gpu, 227, token);
        try
        {
            await writing.Task.WaitAsync(token);
            Need(!authority.WriteStarted && (await control.ApplyLocalAsync(Gpu, 228, token)).Phase == "busy",
                "A local write reserves the controller and never presents a remote command as started.");
            await control.RunAsync(advertised, consent,
                (_, _) => throw new InvalidOperationException("Remote polling raced the local write."), token);
        }
        finally { release.TrySetResult(); }
        Need((await localWrite).Phase == "applied" && control.LastAttempt is null,
            "Finishing a local write leaves the remote outcome untouched.");

        writing = new(TaskCreationOptions.RunContinuationsAsynchronously);
        release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        Task remoteWrite = control.RunAsync(advertised, consent, (receipt, _) =>
        {
            string json = receipt is null ? JsonSerializer.Serialize(new { job = Job(100, 228) })
                : JsonSerializer.SerializeToElement(receipt).GetProperty("phase").GetString() == "claim"
                    ? JsonSerializer.Serialize(new { ok = true, lease_expires = NvidiaPowerControl.Now() + 15 })
                    : """{"ok":true}""";
            return Task.FromResult(JsonDocument.Parse(json));
        }, token);
        try
        {
            await writing.Task.WaitAsync(token);
            Need(authority.WriteStarted && (await control.ApplyLocalAsync(Gpu, 229, token)).Phase == "busy",
                "A remote write reserves the same controller until verification and receipt.");
        }
        finally { release.TrySetResult(); }
        await remoteWrite;
        adapter.OnWriteStarted = null;
        var remote = control.LastAttempt;
        Need(remote is { Phase: "applied", ObservedWatts: 228, Confirmed: true } && !authority.WriteStarted &&
            (await control.ApplyLocalAsync(Gpu, 229, token)).Phase == "applied" && control.LastAttempt == remote,
            "A later local write preserves the previously acknowledged Fleet result.");
    }

    private static object Job(int id, int watts, string gpu = Gpu, long? expires = null) => new
    {
        id = id.ToString("x32"), gpu, watts, expires = expires ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 25,
    };

    private static void Need(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private sealed class Adapter(List<string> calls) : INvidiaPowerAdapter
    {
        internal NvidiaPowerLimit Bounds { get; set; } = new(250, 175, 300);
        internal NvidiaPowerWrite SetResult { get; set; } = NvidiaPowerWrite.Completed;
        internal int Writes { get; private set; }
        internal Func<CancellationToken, Task>? OnWriteStarted { get; set; }
        public bool Available => true;
        public Task<NvidiaPowerLimit?> ReadAsync(string pci, CancellationToken token)
        {
            Need(pci == Gpu, "Select the same exact physical PCI for every read.");
            calls.Add("bounds");
            return Task.FromResult<NvidiaPowerLimit?>(Bounds);
        }
        public async Task<NvidiaPowerWrite> WriteAsync(string pci, int watts, Func<Func<bool>, bool> permitStart,
            CancellationToken token)
        {
            Need(pci == Gpu && watts is >= 175 and <= 300 && permitStart(() => true),
                "Only the exact validated PCI and watts reach the fake setter.");
            calls.Add("write");
            Writes++;
            Bounds = Bounds with { Current = watts };
            if (OnWriteStarted is { } pause) await pause(token);
            return SetResult;
        }
    }

    private sealed class Panel : HttpMessageHandler
    {
        internal List<string> Calls { get; } = [];
        internal object? Job { get; set; }
        internal Action? OnClaim { get; set; }
        internal string? ClaimReply { get; set; }
        internal long? ClaimLease { get; set; }
        internal bool Advertised { get; private set; }
        internal double? Observed { get; private set; }
        internal bool LoseTerminal { get; set; }
        internal bool ExtendedUnsupported { get; set; }
        internal bool DefaultUnsupported { get; set; }
        internal bool ReportedDefault { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            Need(request.RequestUri!.GetLeftPart(UriPartial.Authority) == "https://power.invalid"
                && request.RequestUri.Query == "" && request.Method == HttpMethod.Post
                && !request.Headers.Contains("Authorization") && !request.Headers.Contains("Cookie"), "Secure power endpoint.");
            using var body = JsonDocument.Parse(await request.Content!.ReadAsByteArrayAsync(token));
            string route = request.RequestUri.AbsolutePath;
            var json = body.RootElement;
            if (route == "/api/node/poll")
            {
                Calls.Add("poll");
                var hardware = json.GetProperty("hardware");
                Advertised = hardware.TryGetProperty("power_control", out var flag);
                Need(!Advertised || flag.GetBoolean(), "Omit disabled power control entirely.");
                Need(json.GetProperty("workers").GetArrayLength() == 0, "GPU power remains independent of mining.");
                var gpu = hardware.GetProperty("devices")[0];
                bool hasLimits = gpu.TryGetProperty("power_limit", out var limits);
                ReportedDefault = false;
                if (hasLimits)
                {
                    ReportedDefault = limits.TryGetProperty("default_watts", out var factory);
                    Need(limits.EnumerateObject().Count() == (ReportedDefault ? 4 : 3) &&
                        limits.GetProperty("current").GetDouble() == 250 &&
                        limits.GetProperty("min").GetDouble() == 175 && limits.GetProperty("max").GetDouble() == 300 &&
                        (!ReportedDefault || factory.GetDouble() == 250), "Report the valid optional range in exact wire fields.");
                }
                if (DefaultUnsupported && ReportedDefault) return Reply("{}", HttpStatusCode.BadRequest);
                if (ExtendedUnsupported && (Advertised || hasLimits))
                    return Reply("{}", HttpStatusCode.Conflict);
                return Reply("""{"commands":[]}""");
            }
            if (route == "/api/node/pair") return Reply("""{"ok":true}""");
            if (route == "/api/node/check") return Reply("""{"ok":true,"last_seen":1797000000}""");
            Need(route == "/api/node/power" && json.EnumerateObject().Count() == 3 &&
                json.GetProperty("id").GetString() == Node && json.GetProperty("key").GetString()!.Length == 43,
                "Use the exact authenticated power envelope.");
            var receipt = json.GetProperty("receipt");
            if (receipt.ValueKind == JsonValueKind.Null)
            {
                Calls.Add("job");
                return Reply(JsonSerializer.Serialize(new { job = Job }));
            }
            string phase = receipt.GetProperty("phase").GetString()!;
            Calls.Add(phase);
            if (phase == "claim")
            {
                Need(receipt.EnumerateObject().Count() == 2, "Claim includes only job id and phase.");
                OnClaim?.Invoke();
                return Reply(ClaimReply ?? JsonSerializer.Serialize(new
                {
                    ok = true, lease_expires = ClaimLease ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 15,
                }));
            }
            Need(receipt.EnumerateObject().Count() == 3, "Terminal receipt carries observed hardware.");
            var watts = receipt.GetProperty("observed_watts");
            Observed = watts.ValueKind == JsonValueKind.Null ? null : watts.GetDouble();
            if (LoseTerminal)
            {
                LoseTerminal = false;
                throw new OperationCanceledException("Fixture lost final acknowledgement.");
            }
            return Reply("""{"ok":true}""");
        }

        private static HttpResponseMessage Reply(string json, HttpStatusCode status = HttpStatusCode.OK) =>
            new(status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };
    }
}
