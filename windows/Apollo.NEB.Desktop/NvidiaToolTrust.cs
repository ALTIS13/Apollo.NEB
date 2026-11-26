using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace Apollo.Neb.Desktop;

internal static class NvidiaToolTrust
{
    private static readonly Guid Authenticode = new("00AAC56B-CD44-11D0-8CC2-00C04FC295EE");

    internal static bool IsTrusted(string path)
    {
        IntPtr name = Marshal.StringToHGlobalUni(path);
        IntPtr file = Marshal.AllocHGlobal(Marshal.SizeOf<WinTrustFile>());
        try
        {
            Marshal.StructureToPtr(new WinTrustFile
            {
                Size = (uint)Marshal.SizeOf<WinTrustFile>(),
                Path = name,
            }, file, false);
            var data = new WinTrustData
            {
                Size = (uint)Marshal.SizeOf<WinTrustData>(),
                Ui = 2, // WTD_UI_NONE
                Choice = 1, // WTD_CHOICE_FILE
                File = file,
                StateAction = 1, // WTD_STATEACTION_VERIFY
                Flags = 0x1000, // WTD_CACHE_ONLY_URL_RETRIEVAL
            };
            Guid policy = Authenticode;
            try
            {
                if (WinVerifyTrust(new IntPtr(-1), ref policy, ref data) != 0) return false;
                using var signed = X509Certificate.CreateFromSignedFile(path);
                using var publisher = new X509Certificate2(signed);
                return publisher.GetNameInfo(X509NameType.SimpleName, forIssuer: false) == "NVIDIA Corporation";
            }
            finally
            {
                data.StateAction = 2; // WTD_STATEACTION_CLOSE
                _ = WinVerifyTrust(new IntPtr(-1), ref policy, ref data);
            }
        }
        catch (Exception error) when (error is CryptographicException or IOException or UnauthorizedAccessException)
        {
            return false;
        }
        finally
        {
            Marshal.FreeHGlobal(file);
            Marshal.FreeHGlobal(name);
        }
    }

    [DllImport("wintrust.dll", ExactSpelling = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern int WinVerifyTrust(IntPtr window, ref Guid action, ref WinTrustData data);

    [StructLayout(LayoutKind.Sequential)]
    private struct WinTrustFile
    {
        internal uint Size;
        internal IntPtr Path;
        internal IntPtr File;
        internal IntPtr KnownSubject;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WinTrustData
    {
        internal uint Size;
        internal IntPtr PolicyData;
        internal IntPtr SipData;
        internal uint Ui;
        internal uint RevocationChecks;
        internal uint Choice;
        internal IntPtr File;
        internal uint StateAction;
        internal IntPtr State;
        internal IntPtr UrlReference;
        internal uint Flags;
        internal uint UiContext;
        internal IntPtr SignatureSettings;
    }
}
