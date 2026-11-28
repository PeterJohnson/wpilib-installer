using System.Text.Json;
using System.Net;

internal sealed class FixtureTransport
{
    private readonly string origin, responses, mode;
    public int Requests { get; private set; }
    public FixtureTransport(string origin, string state, string mode)
    { this.origin=Path.GetFullPath(origin); this.responses=Path.Combine(Path.GetFullPath(state), "responses"); this.mode=mode; }
    private static string Under(string root, string relative)
    {
        var p=Path.GetFullPath(Path.Combine(root, relative));
        if(!p.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.Ordinal)) throw new IOException("Fixture path escaped root");
        return p;
    }
    public byte[]? Read(string path)
    {
        Requests++;
        var saved=Under(responses,path);
        if(mode=="cache") return File.Exists(saved)?File.ReadAllBytes(saved):null;
        var p=Under(origin,path);
        if(!File.Exists(p)) return null;
        var data=File.ReadAllBytes(p);Directory.CreateDirectory(Path.GetDirectoryName(saved)!);File.WriteAllBytes(saved,data);return data;
    }
}
internal static class Output
{
    public static int Write(byte[]? catalog, Exception? error, int requests)
    {
        using var w=new Utf8JsonWriter(Console.OpenStandardOutput());w.WriteStartObject();w.WriteString("status",error is null?"ok":"error");w.WriteNumber("requests",requests);
        if(error is not null){w.WriteString("error_type",error.GetType().FullName);w.WriteString("error",error.Message);}
        if(catalog is not null){w.WritePropertyName("catalog");using var d=JsonDocument.Parse(catalog);d.RootElement.WriteTo(w);}
        w.WriteEndObject();w.Flush();return error is null?0:1;
    }
}
