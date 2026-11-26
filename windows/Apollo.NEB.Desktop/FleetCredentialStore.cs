using System.Security.Cryptography;
using System.Text.Json;
using Windows.Security.Cryptography;
using Windows.Security.Cryptography.DataProtection;

namespace Apollo.Neb.Desktop;

internal sealed class FleetCredentialStore(string path) : IDisposable
{
    private const int FileLimit = 16 * 1024;
    private FileStream? _lease;

    internal static FleetCredentialStore ForCurrentUser() =>
        new(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Apollo.NEB.Desktop", "fleet.dat"));

    internal async Task<FleetIdentity?> LoadAsync(bool reserve, CancellationToken token)
    {
        if (!reserve && _lease is null && !File.Exists(path)) return null;
        Reserve();
        byte[] blob;
        try
        {
            using var stream = File.OpenRead(path);
            if (stream.Length is < 1 or > FileLimit) throw InvalidFile();
            blob = new byte[(int)stream.Length];
            await stream.ReadExactlyAsync(blob, token).ConfigureAwait(false);
        }
        catch (FileNotFoundException) { return null; }

        try
        {
            var buffer = await new DataProtectionProvider().UnprotectAsync(CryptographicBuffer.CreateFromByteArray(blob))
                .AsTask(token).ConfigureAwait(false);
            CryptographicBuffer.CopyToByteArray(buffer, out byte[] bytes);
            try
            {
                var identity = JsonSerializer.Deserialize<FleetIdentity>(bytes);
                return identity is not null && FleetLink.IsStoredIdentity(identity) ? identity : throw InvalidFile();
            }
            finally { CryptographicOperations.ZeroMemory(bytes); }
        }
        catch (Exception error) when (error is JsonException or CryptographicException
            or System.Runtime.InteropServices.COMException)
        {
            throw InvalidFile();
        }
    }

    internal async Task SaveAsync(FleetIdentity identity, CancellationToken token)
    {
        Reserve();
        byte[] data = JsonSerializer.SerializeToUtf8Bytes(identity);
        byte[] blob;
        try
        {
            var protectedData = await new DataProtectionProvider("LOCAL=user")
                .ProtectAsync(CryptographicBuffer.CreateFromByteArray(data)).AsTask(token).ConfigureAwait(false);
            CryptographicBuffer.CopyToByteArray(protectedData, out blob);
            if (blob.Length is < 1 or > FileLimit) throw InvalidFile();
        }
        finally { CryptographicOperations.ZeroMemory(data); }

        string temp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var file = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                await file.WriteAsync(blob, token).ConfigureAwait(false);
                file.Flush(flushToDisk: true);
            }
            token.ThrowIfCancellationRequested();
            File.Move(temp, path, overwrite: true);
        }
        finally { File.Delete(temp); }
    }

    internal void Forget()
    {
        Reserve();
        File.Delete(path);
        Dispose();
    }

    private void Reserve()
    {
        if (_lease is not null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        _lease = new FileStream(path + ".lock", FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
    }

    private static InvalidDataException InvalidFile() => new("Fleet identity could not be opened.");

    public void Dispose()
    {
        _lease?.Dispose();
        _lease = null;
    }
}
