using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Xml;
using System.Xml.Linq;

namespace Wpilib.Installer;

public sealed class Workspace : IDisposable
{
    public string Path { get; }

    public Workspace() => Path = Directory.CreateTempSubdirectory("wpilib-manager-").FullName;

    public void Dispose()
    {
        try
        {
            Directory.Delete(Path, true);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}

public static class Files
{
    public static string Text(JsonNode? n) =>
        n?.GetValue<string>() ?? throw new IOException("Expected string");

    public static string Hash(string path)
    {
        RejectLink(path);
        using var f = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(f));
    }

    public static void RejectLink(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
        {
            throw new IOException("Symbolic link/reparse point rejected: " + path);
        }
    }

    public static void Safe(string name)
    {
        Archive.Require(
            !string.IsNullOrEmpty(name) && !name.Any(c => c < ' ' || "\\:%?#".Contains(c)),
            "Unsafe path: " + name
        );
        foreach (var p in name.Split('/'))
        {
            var b = p.Split('.')[0].ToUpperInvariant();
            Archive.Require(
                p is not ("" or "." or "..")
                    && !p.EndsWith('.')
                    && !p.EndsWith(' ')
                    && b is not ("CON" or "PRN" or "AUX" or "NUL")
                    && !(
                        b.Length == 4
                        && (b.StartsWith("COM") || b.StartsWith("LPT"))
                        && b[3] is >= '1' and <= '9'
                    ),
                "Unsafe path: " + name
            );
        }
    }

    public static Version Version(string value)
    {
        var parts = value.Split('.');
        Archive.Require(
            parts.Length is > 0 and <= 4
                && parts.All(p => p.Length > 0 && p.All(char.IsAsciiDigit)),
            "Invalid package version"
        );
        var n = parts
            .Select(p => int.Parse(p, System.Globalization.CultureInfo.InvariantCulture))
            .Concat([0, 0, 0, 0])
            .Take(4)
            .ToArray();
        return new Version(n[0], n[1], n[2], n[3]);
    }

    public static XDocument Xml(string path)
    {
        using var reader = XmlReader.Create(
            path,
            new XmlReaderSettings
            {
                DtdProcessing = DtdProcessing.Prohibit,
                XmlResolver = null,
                MaxCharactersInDocument = Storage.Limit,
            }
        );
        return XDocument.Load(reader);
    }

    public static SortedDictionary<string, string> Inventory(string root)
    {
        var result = new SortedDictionary<string, string>(StringComparer.Ordinal);
        var p = System.IO.Path.Combine(root, "components.xml");
        if (!File.Exists(p))
        {
            return result;
        }
        foreach (var package in Xml(p).Root!.Elements("Package"))
        {
            var id = package.Element("Name")!.Value;
            var v = package.Element("Version")!.Value;
            Version(v);
            Archive.Require(result.TryAdd(id, v), "Duplicate installed component");
        }
        return result;
    }

    public static JsonObject Json(IEnumerable<KeyValuePair<string, string>> values)
    {
        var r = new JsonObject();
        foreach (var (k, v) in values)
        {
            r[k] = v;
        }
        return r;
    }

    public static void Extract(Stream stream, string destination, bool executable)
    {
        using var zip = new ZipArchive(stream, ZipArchiveMode.Read, leaveOpen: true);
        Archive.Require(zip.Entries.Count <= 200000, "Too many archive members");
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long total = 0;
        foreach (var e in zip.Entries)
        {
            var name = e.FullName.TrimEnd('/');
            Safe(name);
            var mode = (e.ExternalAttributes >> 16) & 0xffff;
            Archive.Require(
                (mode & 0xf000) is 0 or 0x8000 or 0x4000,
                "Archive special file rejected"
            );
            Archive.Require(seen.Add(name), "Duplicate archive member");
            total = checked(total + e.Length);
            Archive.Require(total <= 100L * 1024 * 1024 * 1024, "Archive exceeds 100 GiB");
            var path = Storage.Under(destination, name);
            if (e.FullName.EndsWith('/'))
            {
                Directory.CreateDirectory(path);
                continue;
            }
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
            using (
                var output = new FileStream(
                    path,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None
                )
            )
            using (var input = e.Open())
            {
                var buffer = new byte[65536];
                long written = 0;
                int count;
                while ((count = input.Read(buffer)) > 0)
                {
                    written = checked(written + count);
                    Archive.Require(written <= e.Length, "Oversized ZIP member");
                    output.Write(buffer, 0, count);
                }
                Archive.Require(written == e.Length, "Truncated ZIP member");
            }
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(
                    path,
                    executable && (mode & 73) != 0 ? (UnixFileMode)448 : (UnixFileMode)384
                );
            }
        }
    }

    public static void Verify(string path, JsonNode record)
    {
        var hash = Text(record["sha256"]);
        Archive.Require(
            hash.Length == 64 && hash.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f'),
            "Invalid file hash"
        );
        RejectLink(path);
        Archive.Require(
            new FileInfo(path).Length == record["size"]!.GetValue<long>() && Hash(path) == hash,
            "Corrupt repository file: " + path
        );
    }

    public static void CopyAtomic(string source, string destination)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(destination)!);
        var temporary = destination + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var src = File.OpenRead(source))
            using (
                var dest = new FileStream(
                    temporary,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None
                )
            )
            {
                src.CopyTo(dest);
                dest.Flush(true);
            }
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(temporary, File.GetUnixFileMode(source));
            }
            File.Move(temporary, destination, true);
        }
        finally
        {
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
        }
    }
}
