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
    var exe = Path.Combine(root, "manager-experiment" + (OperatingSystem.IsWindows() ? ".exe" : ""));
    var command = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true };
    foreach (var arg in args) command.ArgumentList.Add(arg);
    using var child = Process.Start(command)!; child.WaitForExit(); return child.ExitCode;
}
finally { Directory.Delete(root, true); }
