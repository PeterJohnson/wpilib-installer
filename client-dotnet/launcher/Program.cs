using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
// One public executable; native GUI libraries and Qt stay in a private per-run directory.
// Fresh extraction avoids treating a writable persistent executable cache as trusted.
var root = Directory.CreateTempSubdirectory("wpilib-csharp-app-").FullName;
try
{
    using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("application.zip")!;
    using var archive = new ZipArchive(stream);
    foreach (var entry in archive.Entries)
    {
        var path = Path.GetFullPath(Path.Combine(root, entry.FullName));
        if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal)) throw new IOException("invalid compiled payload path");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!); entry.ExtractToFile(path);
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(path, (UnixFileMode)((entry.ExternalAttributes >> 16) & 511));
    }
    var exe = Path.Combine(root, "wpilib-client" + (OperatingSystem.IsWindows() ? ".exe" : ""));
    var command = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
    command.Environment["WPILIB_MANAGER_APPLICATION"] = Environment.ProcessPath!;
    if (OperatingSystem.IsLinux()) command.Environment["LD_LIBRARY_PATH"] = root;
    foreach (var arg in args) command.ArgumentList.Add(arg);
    using var child = Process.Start(command)!;
    var stdout = child.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
    var stderr = child.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError());
    await child.WaitForExitAsync(); await Task.WhenAll(stdout, stderr); return child.ExitCode;
}
finally
{
    if (!OperatingSystem.IsWindows()) Directory.Delete(root, true);
    else
    {
        // Windows may signal process exit before image mappings/AV handles release.
        // Cleanup must not replace an already-completed CLI result with fail-fast.
        for (var attempt = 0; attempt < 8; attempt++)
        {
            try { Directory.Delete(root, true); break; }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                if (attempt == 7) Console.Error.WriteLine($"Temporary application cleanup deferred: {e.Message}");
                else await Task.Delay(100 * (attempt + 1));
            }
        }
    }
}
