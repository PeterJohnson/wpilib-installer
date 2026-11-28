using System.Security.Cryptography;
using System.Text.Json.Nodes;

namespace Wpilib.Installer;

public static class Storage
{
    public const int Limit = 8 * 1024 * 1024;

    public static byte[] Read(string path)
    {
        using var stream = File.OpenRead(path);
        if (stream.Length > Limit)
        {
            throw new IOException("metadata exceeds 8 MiB");
        }
        using var result = new MemoryStream();
        stream.CopyTo(result);
        if (result.Length > Limit)
        {
            throw new IOException("metadata exceeds 8 MiB");
        }
        return result.ToArray();
    }

    public static JsonObject Object(byte[] bytes) =>
        JsonNode.Parse(bytes) as JsonObject ?? throw new IOException("expected JSON object");

    public static JsonObject Object(string path) => Object(Read(path));

    public static void Write(string path, byte[] bytes)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (
                var stream = new FileStream(
                    temporary,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None
                )
            )
            {
                stream.Write(bytes);
                stream.Flush(true);
            }
            File.Move(temporary, path, true);
        }
        finally
        {
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
        }
    }

    public static string Hash(byte[] bytes) => Convert.ToHexStringLower(SHA256.HashData(bytes));

    public static string Under(string directory, string relative)
    {
        if (relative.Contains('\\') || relative.Split('/').Any(p => p is "" or "." or ".."))
        {
            throw new IOException("unsafe relative path");
        }
        var root = Path.GetFullPath(directory);
        var path = Path.GetFullPath(Path.Combine(root, relative));
        if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal))
        {
            throw new IOException("path escaped root");
        }
        return path;
    }
}
