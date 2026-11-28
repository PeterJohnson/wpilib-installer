using System.Net;
using System.Text.Json.Nodes;
using Tuf;
namespace Wpilib.Installer;

// Policy adapter, not a second implementation of TUF signature/rotation verification.
public sealed class Online : ITufCache, ITufRepository, IDisposable
{
    readonly byte[] bootstrap;
    readonly string origin, state, mode;
    readonly int budget;
    readonly FileStream gate;
    readonly HttpClient http = new(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(30) };
    readonly JsonObject floors;
    readonly int rootFloor;
    bool reachedEnd;
    JsonObject currentRoot;
    public int RootVersion { get; private set; }
    public int NewRoots { get; private set; }
    public Online(byte[] root, string origin, string state, string mode, int budget)
    {
        currentRoot = Storage.Object(root); bootstrap = root; this.origin = origin; this.state = Path.GetFullPath(state); this.mode = mode; this.budget = budget;
        Directory.CreateDirectory(state);
        gate = new FileStream(Path.Combine(state, ".lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        try
        {
            var pin = Path.Combine(state, "bootstrap.json");
            if (File.Exists(pin) && Storage.Hash(Archive.Canonical(Storage.Object(pin))) != Storage.Hash(Archive.Canonical(Storage.Object(root))))
                throw new IOException("cache bootstrap mismatch");
            Storage.Write(pin, root);
            RootVersion = Storage.Object(root)["signed"]!["version"]!.GetValue<int>();
            floors = File.Exists(FloorsPath) ? Storage.Object(FloorsPath) : new JsonObject();
            rootFloor = floors["root"]?.GetValue<int>() ?? RootVersion;
        }
        catch { gate.Dispose(); http.Dispose(); throw; }
    }
    string FloorsPath => Path.Combine(state, "floors.json");
    public async Task<JsonObject> Fetch(int limit = int.MaxValue)
    {
        using var client = new TufClient(new TufClientOptions {
            TrustedRoot = bootstrap, MetadataBaseUrl = new Uri("https://unused.invalid/metadata/"),
            Cache = this, Repository = this, MaxRootRotations = limit == 0 ? 1024 : limit });
        var result = await client.GetTargetAsync("catalog.json");
        if (!reachedEnd) throw new IOException("root-update-pending");
        return new JsonObject { ["status"] = "ok", ["catalog"] = JsonNode.Parse(result.Content.Span), ["catalog_sha256"] = Storage.Hash(result.Content.ToArray()), ["root_version"] = RootVersion, ["new_roots"] = NewRoots };
    }
    public byte[]? LoadMetadata(string role)
    {
        // Always replay from the compiled pin. Library-local roots never become new anchors.
        if (role == "root") return bootstrap;
        var p = Storage.Under(Path.Combine(state, "metadata"), role + ".json");
        return File.Exists(p) ? Storage.Read(p) : null;
    }
    public void StoreMetadata(string role, byte[] data)
    {
        var version = Storage.Object(data)["signed"]!["version"]!.GetValue<int>();
        if (role == "root")
        {
            var nextRoot = Storage.Object(data);
            if (version > rootFloor && new[] { "timestamp", "snapshot" }.Any(role => !JsonNode.DeepEquals(TrustHistory.RoleKeys(currentRoot, role), TrustHistory.RoleKeys(nextRoot, role))))
            {
                foreach (var r in new[] { "timestamp", "snapshot", "targets" })
                { floors.Remove(r); var old = Path.Combine(state, "metadata", r + ".json"); if (File.Exists(old)) File.Delete(old); }
            }
            currentRoot = nextRoot;
            Storage.Write(Path.Combine(state, "metadata", "root.json"), data);
            RootVersion = version;
            var first = Storage.Object(bootstrap)["signed"]!["version"]!.GetValue<int>();
            if (version > first)
                Storage.Write(Storage.Under(Path.Combine(state, "responses"), $"metadata/{version}.root.json"), data);
            // Earlier roots in the authenticated replay must not lower the saved checkpoint.
            floors[role] = Math.Max(version, floors[role]?.GetValue<int>() ?? 0);
        }
        else
        {
            if (version < (floors[role]?.GetValue<int>() ?? 0)) throw new IOException(role + " rollback checkpoint");
            floors[role] = version;
            Storage.Write(Storage.Under(Path.Combine(state, "metadata"), role + ".json"), data);
        }
        Storage.Write(FloorsPath, Archive.Canonical(floors));
    }
    // Force target/metadata evidence through the verifier on every cache replay.
    public byte[]? LoadTarget(string path) => null;
    public void StoreTarget(string path, byte[] data) { }
    public async Task<byte[]?> FetchMetadataAsync(string role, int? version = null, CancellationToken cancellationToken = default)
    {
        if (role == "root")
        {
            var relative = $"metadata/{version}.root.json";
            var saved = Storage.Under(Path.Combine(state, "responses"), relative);
            if (File.Exists(saved)) return Storage.Read(saved);
            if (version <= rootFloor) throw new IOException("missing authenticated root history below checkpoint");
            if (mode != "cache" && budget > 0 && NewRoots >= budget) throw new IOException("root-update-pending");
            var data = await Read(relative, cancellationToken, save: false);
            if (data is null) { reachedEnd = true; return null; }
            NewRoots++;
            // TufClient calls StoreMetadata only after both thresholds/version verification.
            return data;
        }
        if (!reachedEnd) throw new IOException("root-update-pending");
        var result = await Read("metadata/" + (version is null ? "" : version + ".") + role + ".json", cancellationToken);
        if (result is not null && Storage.Object(result)["signed"]!["version"]!.GetValue<int>() < (floors[role]?.GetValue<int>() ?? 0))
            throw new IOException(role + " rollback checkpoint");
        return result;
    }
    public Task<byte[]?> FetchTargetAsync(string targetPath, CancellationToken cancellationToken = default)
        => Read("targets/" + targetPath, cancellationToken);
    async Task<byte[]?> Read(string relative, CancellationToken token, bool save = true)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(120)); token = deadline.Token;
        var saved = Storage.Under(Path.Combine(state, "responses"), relative);
        if (mode == "cache") return File.Exists(saved) ? Storage.Read(saved) : null;
        byte[] data;
        if (Uri.TryCreate(origin, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https")
        {
            if (uri.Scheme != "https" && !uri.IsLoopback) throw new IOException("HTTPS required");
            using var response = await http.GetAsync(new Uri(uri.AbsoluteUri.TrimEnd('/') + "/" + relative), HttpCompletionOption.ResponseHeadersRead, token);
            if (response.StatusCode == HttpStatusCode.NotFound) return null;
            response.EnsureSuccessStatusCode();
            if (response.Content.Headers.ContentLength > Storage.Limit) throw new IOException("metadata exceeds 8 MiB");
            await using var stream = await response.Content.ReadAsStreamAsync(token);
            using var bytes = new MemoryStream(); var buffer = new byte[65536]; int count;
            while ((count = await stream.ReadAsync(buffer, token)) > 0)
            { if (bytes.Length + count > Storage.Limit) throw new IOException("metadata exceeds 8 MiB"); bytes.Write(buffer, 0, count); }
            data = bytes.ToArray();
        }
        else
        {
            var local = uri?.IsFile == true ? uri.LocalPath : origin;
            var p = Storage.Under(local, relative); if (!File.Exists(p)) return null; data = Storage.Read(p);
        }
        if (save) Storage.Write(saved, data);
        return data;
    }
    public void Dispose() { http.Dispose(); gate.Dispose(); }
}
