using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.VisualBasic.FileIO;

namespace Apollo.Neb.Desktop;

internal sealed record NvidiaGpuReading(string Name, string BusId, double? Temperature, double? Utilization,
    long? MemoryUsedMiB, long? MemoryTotalMiB, double? Watts, NvidiaPowerLimit? PowerLimit = null);

internal enum NvidiaQueryState { Ready, NoDevices, ToolMissing, Unavailable }

internal sealed record NvidiaSnapshot(NvidiaQueryState State, IReadOnlyList<NvidiaGpuReading> Devices);

internal static class NvidiaTelemetry
{
    private const int MaxOutputBytes = 32 * 1024;
    private const int MaxErrorBytes = 4 * 1024;
    private const string Query = "--query-gpu=name,pci.bus_id,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw";
    private const string PowerQuery = "--query-gpu=pci.bus_id,power.limit,power.min_limit,power.max_limit";
    private const string PowerQueryWithDefault = PowerQuery + ",power.default_limit";
    private const string CsvFormat = "--format=csv,noheader,nounits";
    private static readonly Regex PciAddress = new(@"\A[0-9a-f]{4}(?:[0-9a-f]{4})?:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\z",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    internal static async Task<NvidiaSnapshot> ReadAsync(CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        string? executable = FindTool();
        if (executable is null) return new(NvidiaQueryState.ToolMissing, []);

        var result = await RunAsync(executable, [Query, CsvFormat], token).ConfigureAwait(false);
        if (IsNoDevices(result.Output) || IsNoDevices(result.Error)) return new(NvidiaQueryState.NoDevices, []);
        if (result.ExitCode != 0) return new(NvidiaQueryState.Unavailable, []);
        try
        {
            var devices = Parse(result.Output);
            if (devices.Count == 0) return new(NvidiaQueryState.NoDevices, []);
            var limits = await QueryPowerLimitsAsync(executable, null, token).ConfigureAwait(false);
            return new(NvidiaQueryState.Ready, devices.Select(gpu =>
                gpu with { PowerLimit = limits.GetValueOrDefault(CanonicalAddress(gpu.BusId)) }).ToArray());
        }
        catch (InvalidDataException) { return new(NvidiaQueryState.Unavailable, []); }
    }

    private static async Task<Dictionary<string, NvidiaPowerLimit>> QueryPowerLimitsAsync(
        string executable, string? pci, CancellationToken token)
    {
        string[] args = pci is null ? [PowerQueryWithDefault, CsvFormat] : ["-i", pci, PowerQuery, CsvFormat];
        var result = await RunAsync(executable, args, token).ConfigureAwait(false);
        if (pci is null && result.ExitCode != 0)
            result = await RunAsync(executable, [PowerQuery, CsvFormat], token).ConfigureAwait(false);
        return result.ExitCode == 0 ? ParsePowerLimits(result.Output) : new();
    }

    internal static Dictionary<string, NvidiaPowerLimit> ParsePowerLimits(string csv)
    {
        var found = new Dictionary<string, NvidiaPowerLimit>();
        var seen = new HashSet<string>();
        using var reader = new StringReader(csv);
        string? row;
        while ((row = reader.ReadLine()) is not null)
        {
            var fields = row.Split(',', StringSplitOptions.TrimEntries);
            if (fields.Length is not (4 or 5) || !PciAddress.IsMatch(fields[0])) continue;
            string id = CanonicalAddress(fields[0]);
            if (!seen.Add(id))
            {
                found.Remove(id);
                continue;
            }
            if (!PowerNumber(fields[1], out var current) || !PowerNumber(fields[2], out var min)
                || !PowerNumber(fields[3], out var max) || new NvidiaPowerLimit(current, min, max) is not { IsValid: true } limits)
                continue;
            if (fields.Length == 5 && PowerNumber(fields[4], out var factory) && factory >= min && factory <= max)
                limits = limits with { DefaultWatts = factory };
            found.Add(id, limits);
        }
        return found;
    }

    private static bool PowerNumber(string field, out double number) =>
        double.TryParse(field, NumberStyles.Float, CultureInfo.InvariantCulture, out number) && double.IsFinite(number);

    internal static string CanonicalAddress(string busId) => busId.PadLeft(16, '0').ToLowerInvariant();

    private static async Task<ToolReply> RunAsync(string executable, IReadOnlyList<string> args,
        CancellationToken token, Func<Func<bool>, bool>? permittedStart = null, Action? onUnsettledWrite = null)
    {
        token.ThrowIfCancellationRequested();
        var settings = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(executable)!,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (string arg in args) settings.ArgumentList.Add(arg);

        using var process = new Process { StartInfo = settings };
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(4));
        bool started = false;
        try
        {
            started = permittedStart is null ? process.Start() : permittedStart(process.Start);
            if (!started) return new(false, null);
            Task<string> output = ReadBoundedAsync(process.StandardOutput.BaseStream, MaxOutputBytes, deadline.Token);
            Task<string> error = ReadBoundedAsync(process.StandardError.BaseStream, MaxErrorBytes, deadline.Token);
            await Task.WhenAll(output, error, process.WaitForExitAsync(deadline.Token))
                .WaitAsync(deadline.Token).ConfigureAwait(false);
            return new(true, process.ExitCode, await output.ConfigureAwait(false), await error.ConfigureAwait(false));
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { throw; }
        catch (Exception error) when (error is OperationCanceledException or Win32Exception
                                        or IOException or InvalidDataException or InvalidOperationException)
        {
            return new(started, null);
        }
        finally
        {
            if (started && !EndOwnProcess(process, waitForExit: onUnsettledWrite is not null))
                onUnsettledWrite?.Invoke();
        }
    }

    internal sealed class PowerAdapter : INvidiaPowerAdapter
    {
        private volatile bool _unsettledWrite;
        public bool Available => !_unsettledWrite && FindTool(requireSignature: true) is not null;

        public async Task<NvidiaPowerLimit?> ReadAsync(string pci, CancellationToken token)
        {
            if (!NvidiaPowerControl.IsGpu(pci) || FindTool(requireSignature: true) is not { } executable)
                return null;
            return (await QueryPowerLimitsAsync(executable, pci, token).ConfigureAwait(false)).GetValueOrDefault(pci);
        }

        public async Task<NvidiaPowerWrite> WriteAsync(string pci, int watts, Func<Func<bool>, bool> permitStart,
            CancellationToken token)
        {
            if (_unsettledWrite || !NvidiaPowerControl.IsGpu(pci) || watts is < 1 or > 3000
                || FindTool(requireSignature: true) is not { } executable)
                return NvidiaPowerWrite.NotStarted;
            var result = await RunAsync(executable, ["-i", pci, "--power-limit", watts.ToString(CultureInfo.InvariantCulture)],
                token, permitStart, () => _unsettledWrite = true).ConfigureAwait(false);
            return result.Started ? (result.ExitCode == 0 ? NvidiaPowerWrite.Completed : NvidiaPowerWrite.Unknown)
                : NvidiaPowerWrite.NotStarted;
        }
    }

    private sealed record ToolReply(bool Started, int? ExitCode, string Output = "", string Error = "");

    private static bool EndOwnProcess(Process process, bool waitForExit)
    {
        try
        {
            if (process.HasExited) return true;
            process.Kill();
        }
        catch (Exception error) when (error is Win32Exception or InvalidOperationException)
        {
            // The child can exit between the check and Kill.
        }
        if (!waitForExit) return true;

        // Do not allow another setter if this process cannot be confirmed stopped.
        try { return process.WaitForExit(1000); }
        catch (Exception error) when (error is Win32Exception or InvalidOperationException) { return false; }
    }

    private static string? FindTool(bool requireSignature = false)
    {
        if (!OperatingSystem.IsWindows()) return null;
        string[] candidates = [
            Path.Combine(Environment.SystemDirectory, "nvidia-smi.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
        ];
        return candidates.FirstOrDefault(path => Path.IsPathFullyQualified(path) && File.Exists(path)
            && (!requireSignature || NvidiaToolTrust.IsTrusted(path)));
    }

    private static bool IsNoDevices(string value) =>
        string.Equals(value.Trim().TrimEnd('.'), "No devices were found", StringComparison.OrdinalIgnoreCase);

    private static async Task<string> ReadBoundedAsync(Stream stream, int maxBytes, CancellationToken token)
    {
        using var result = new MemoryStream();
        byte[] block = new byte[2048];
        int read;
        while ((read = await stream.ReadAsync(block, token).ConfigureAwait(false)) != 0)
        {
            if (result.Length + read > maxBytes)
                throw new InvalidDataException("NVIDIA output exceeds the limit.");
            result.Write(block, 0, read);
        }
        return Encoding.UTF8.GetString(result.GetBuffer(), 0, (int)result.Length);
    }

    internal static IReadOnlyList<NvidiaGpuReading> Parse(string csv)
    {
        using var lines = new StringReader(csv);
        using var reader = new TextFieldParser(lines) { HasFieldsEnclosedInQuotes = true, TrimWhiteSpace = true };
        reader.SetDelimiters(",");
        var result = new List<NvidiaGpuReading>();
        try
        {
            while (!reader.EndOfData)
            {
                var fields = reader.ReadFields();
                if (fields is not { Length: 7 } || fields[0].Length is 0 or > 160
                    || fields[0].Any(char.IsControl) || !PciAddress.IsMatch(fields[1]))
                    throw new InvalidDataException("Invalid NVIDIA GPU identity.");
                result.Add(new NvidiaGpuReading(fields[0], fields[1],
                    Measurement(fields[2], -100, 300), Measurement(fields[3], 0, 100),
                    Memory(fields[4]), Memory(fields[5]), Measurement(fields[6], 0, double.MaxValue)));
            }
        }
        catch (MalformedLineException error)
        {
            throw new InvalidDataException("Invalid NVIDIA CSV.", error);
        }
        return result;
    }

    private static bool Missing(string text) =>
        text.Trim('[', ']').Trim().ToLowerInvariant() is "" or "n/a" or "not available" or "not supported";

    private static double? Measurement(string text, double minimum, double maximum)
    {
        if (Missing(text)) return null;
        if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
            || !double.IsFinite(value) || value < minimum || value > maximum)
            throw new InvalidDataException("Invalid NVIDIA measurement.");
        return value;
    }

    private static long? Memory(string text)
    {
        if (Missing(text)) return null;
        if (!long.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out var value) || value < 0)
            throw new InvalidDataException("Invalid NVIDIA memory.");
        return value;
    }
}
