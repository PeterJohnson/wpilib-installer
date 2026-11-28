using System.Net;
using System.Text.Json.Nodes;

namespace Wpilib.Installer;

public static class OnlinePackages
{
    public static void Monotonic(JsonNode value, JsonNode? previous)
    {
        if (previous is null)
        {
            return;
        }
        long revision = value["signed"]!["revision"]!.GetValue<long>(),
            old = previous["signed"]!["revision"]!.GetValue<long>();
        Archive.Require(
            revision > old || revision == old && JsonNode.DeepEquals(value, previous),
            "Registry/publisher revision rollback or mutation"
        );
    }

    static List<JsonObject> Releases(JsonNode catalog, JsonObject registry, string publisher)
    {
        Archive.Require(
            Files.Text(catalog["kind"]) == "publisher-catalog"
                && catalog["schema"]!.GetValue<int>() == 1
                && Files.Text(catalog["publisher"]) == publisher,
            "Publisher catalog identity mismatch"
        );
        Bundle.Scope(catalog, registry);
        var policy = registry["repositories"]![publisher]!;
        var all = catalog["releases"]!.AsArray();
        Archive.Require(all.Count is > 0 and <= 8, "Invalid retained history");
        long previous = 0;
        var result = new List<JsonObject>();
        foreach (var envelope in all)
        {
            var record = Archive.Signed(envelope!.AsObject(), Files.Text(policy["public_key"]));
            var revision = record["revision"]!.GetValue<long>();
            Archive.Require(
                revision > previous
                    && record["schema"]!.GetValue<int>() == 1
                    && Files.Text(record["publisher"]) == publisher,
                "Invalid publisher history"
            );
            previous = revision;
            Bundle.Scope(record, registry);
            foreach (var (name, file) in record["files"]!.AsObject())
            {
                Files.Safe(name);
                var hash = Files.Text(file!["sha256"]);
                Archive.Require(
                    hash.Length == 64
                        && hash.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f')
                        && file["size"]!.GetValue<long>() >= 0,
                    "Invalid release file record"
                );
            }
            if (revision >= (policy["minimum_revision"]?.GetValue<long>() ?? 1))
            {
                result.Add(envelope.AsObject());
            }
        }
        Archive.Require(result.Count > 0, "No publisher release meets registry floor");
        result.Reverse();
        return result;
    }

    static Dictionary<string, JsonObject> Choose(
        string[] order,
        Dictionary<string, List<JsonObject>> available
    )
    {
        // Try newest releases first in registry-defined publisher order. Fall back through
        // retained releases when dependencies conflict, with a bounded combination search.
        var indices = new int[order.Length];
        for (int attempt = 0; attempt < 4096; attempt++)
        {
            var packages = new JsonObject();
            bool valid = true;
            for (int i = 0; i < order.Length; i++)
            {
                foreach (
                    var package in available[order[i]][indices[i]]["signed"]!["packages"]!.AsArray()
                )
                {
                    var id = Files.Text(package!["id"]);
                    if (packages.ContainsKey(id))
                    {
                        valid = false;
                        break;
                    }
                    packages[id] = package.DeepClone();
                }
            }
            if (valid)
            {
                try
                {
                    Bundle.ValidatePlan(packages);
                    return order
                        .Select((p, i) => (p, release: available[p][indices[i]]))
                        .ToDictionary(x => x.p, x => x.release);
                }
                catch (IOException) { }
            }
            bool carry = true;
            for (int i = order.Length - 1; i >= 0; i--)
            {
                if (++indices[i] < available[order[i]].Count)
                {
                    carry = false;
                    break;
                }
                indices[i] = 0;
            }
            if (carry)
            {
                break;
            }
        }
        throw new IOException("No compatible retained package set");
    }

    public static async Task<Bundle> Load(
        JsonObject source,
        string cache,
        bool cacheOnly,
        Action<string> log
    )
    {
        bool dev = source["allow_loopback_http"]?.GetValue<bool>() == true;
        var catalog = await CatalogStore.Load(source["tuf"]!, cache, dev, cacheOnly, log);
        var record = catalog.Value;
        var signed = record["signed"]!;
        var registry = signed["registry"]!.AsObject();
        Archive.Require(
            record.AsObject().Count == 2
                && record["signature"] is not null
                && Files.Text(signed["kind"]) == "registry"
                && signed["schema"]!.GetValue<int>() == 1
                && JsonNode.DeepEquals(signed["registry_id"], source["registry_id"])
                && registry["schema"]!.GetValue<int>() == 1
                && Files.Text(registry["online_trust"]) == "tuf",
            "Invalid registry identity/schema"
        );
        Bundle.Scope(registry, source);
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        Archive.Require(
            signed["issued_at"]!.GetValue<long>() > 0
                && signed["issued_at"]!.GetValue<long>() <= now
                && signed["expires_at"]!.GetValue<long>() > now
                && signed["revision"]!.GetValue<long>()
                    >= (source["minimum_revision"]?.GetValue<long>() ?? 1),
            "Registry online authorization expired or below revision floor"
        );
        Monotonic(record, catalog.Previous);
        var policies = registry["repositories"]!.AsObject();
        Archive.Require(policies.Count is > 0 and <= 64, "Invalid publisher count");
        var order = registry["repository_order"]?.AsArray().Select(Files.Text).ToArray()
            ?? policies.Select(p => p.Key).Order(StringComparer.Ordinal).ToArray();
        Archive.Require(
            order.Length == policies.Count
                && order.ToHashSet().SetEquals(policies.Select(p => p.Key)),
            "Invalid publisher order"
        );
        var ids = new HashSet<string> { Files.Text(source["tuf"]!["repository_id"]) };
        var available = new Dictionary<string, List<JsonObject>>();
        var catalogs = new List<VerifiedCatalog> { catalog };
        var statuses = new JsonArray(
            new JsonObject
            {
                ["repository"] = "registry",
                ["status"] = catalog.Status,
                ["root_version"] = catalog.RootVersion,
            }
        );
        foreach (var publisher in order)
        {
            Archive.Require(
                publisher.Length > 0
                    && publisher.All(c =>
                        char.IsAsciiLetterLower(c) || char.IsAsciiDigit(c) || c is '_' or '-'
                    ),
                "Invalid publisher identifier"
            );
            var policy = policies[publisher]!;
            CatalogStore.Url(Files.Text(policy["url"]), dev);
            Archive.Require(
                ids.Add(Files.Text(policy["tuf"]!["repository_id"])),
                "Publishers must have independent TUF identities"
            );
            log("Refreshing " + publisher + "…");
            var c = await CatalogStore.Load(policy["tuf"]!, cache, dev, cacheOnly, log);
            var candidates = Releases(c.Value, registry, publisher);
            if (c.Previous is not null)
            {
                Monotonic(
                    c.Value["releases"]!.AsArray().Last()!,
                    c.Previous["releases"]!.AsArray().Last()
                );
            }
            statuses.Add(
                (JsonNode)
                    new JsonObject
                    {
                        ["repository"] = publisher,
                        ["status"] = c.Status,
                        ["root_version"] = c.RootVersion,
                    }
            );
            available[publisher] = candidates;
            catalogs.Add(c);
        }
        var chosen = Choose(order, available);
        var workspace = new Workspace();
        try
        {
            var references = new JsonObject();
            var remote = new JsonObject();
            var held = new JsonArray();
            foreach (var (name, envelope) in chosen)
            {
                var directory = Path.Combine(workspace.Path, "repos", name);
                Directory.CreateDirectory(directory);
                Storage.Write(Path.Combine(directory, "release.json"), Archive.Canonical(envelope));
                var m = envelope["signed"]!;
                references[name] = new JsonObject
                {
                    ["revision"] = m["revision"]!.DeepClone(),
                    ["sha256"] = Storage.Hash(Archive.Canonical(envelope)),
                };
                var url =
                    Files.Text(policies[name]!["url"]).TrimEnd('/') + "/releases/" + m["revision"];
                await VerifiedFile(
                    url,
                    "Updates.xml",
                    m["files"]!["Updates.xml"]!,
                    cache,
                    Path.Combine(directory, "Updates.xml"),
                    dev,
                    cacheOnly
                );
                remote[name] = new JsonObject
                {
                    ["url"] = url,
                    ["files"] = m["files"]!.DeepClone(),
                };
                if (!JsonNode.DeepEquals(m["revision"], available[name][0]["signed"]!["revision"]))
                {
                    held.Add(
                        (JsonNode)
                            new JsonObject
                            {
                                ["publisher"] = name,
                                ["selected"] = m["revision"]!.DeepClone(),
                                ["latest"] = available[name][0]["signed"]!["revision"]!.DeepClone(),
                            }
                    );
                }
            }
            var identity = new JsonObject
            {
                ["registry_id"] = source["registry_id"]!.DeepClone(),
                ["registry_revision"] = signed["revision"]!.DeepClone(),
                ["registry_sha256"] = Storage.Hash(Archive.Canonical(record)),
                ["status"] = catalogs.Any(c => c.Status == "verified-cache")
                    ? "verified-cache"
                    : "verified-online",
                ["repositories"] = statuses,
                ["held_back"] = held,
            };
            var bundle = Bundle.Load(
                workspace,
                workspace.Path,
                registry,
                identity,
                references,
                true,
                log
            );
            bundle.Remote = remote;
            // Advance application-level acceptance only after the combined catalog validates.
            // TUF rollback checkpoints may already have advanced during metadata refresh.
            foreach (var c in catalogs)
            {
                c.Accept();
            }
            return bundle;
        }
        catch
        {
            workspace.Dispose();
            throw;
        }
    }

    static async Task VerifiedFile(
        string baseUrl,
        string name,
        JsonNode record,
        string cache,
        string destination,
        bool development,
        bool cacheOnly
    )
    {
        Files.Safe(name);
        var hash = Files.Text(record["sha256"]);
        Archive.Require(
            hash.Length == 64 && hash.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f'),
            "Invalid payload hash"
        );
        long size = record["size"]!.GetValue<long>();
        Archive.Require(size is >= 0 and <= 32L * 1024 * 1024 * 1024, "Invalid payload size");
        var cached = Path.Combine(cache, "objects", hash);
        if (File.Exists(cached))
        {
            Files.Verify(cached, record);
        }
        else
        {
            Archive.Require(!cacheOnly, "Verified payload is not cached: " + name);
            var url = new Uri(CatalogStore.Url(baseUrl.TrimEnd('/') + "/", development), name);
            using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false });
            using var deadline = new CancellationTokenSource(TimeSpan.FromMinutes(10));
            using var response = await client.GetAsync(
                url,
                HttpCompletionOption.ResponseHeadersRead,
                deadline.Token
            );
            response.EnsureSuccessStatusCode();
            Archive.Require(
                response.Content.Headers.ContentLength is null
                    || response.Content.Headers.ContentLength == size,
                "Payload size mismatch"
            );
            Directory.CreateDirectory(Path.GetDirectoryName(cached)!);
            var temporary = cached + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                await using (var stream = await response.Content.ReadAsStreamAsync(deadline.Token))
                using (
                    var output = new FileStream(
                        temporary,
                        FileMode.CreateNew,
                        FileAccess.Write,
                        FileShare.None
                    )
                )
                {
                    var buffer = new byte[65536];
                    long length = 0;
                    int n;
                    while ((n = await stream.ReadAsync(buffer, deadline.Token)) > 0)
                    {
                        length = checked(length + n);
                        Archive.Require(length <= size, "Oversized package download");
                        output.Write(buffer, 0, n);
                    }
                    Archive.Require(length == size, "Truncated package download");
                    output.Flush(true);
                }
                Files.Verify(temporary, record);
                File.Move(temporary, cached, true);
            }
            finally
            {
                if (File.Exists(temporary))
                {
                    File.Delete(temporary);
                }
            }
        }
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        File.Copy(cached, destination, true);
        Files.Verify(destination, record);
    }

    public static async Task Materialize(
        Bundle bundle,
        HashSet<string> selected,
        string cache,
        JsonObject source,
        bool cacheOnly,
        Action<string> log
    )
    {
        foreach (var (publisher, remote) in bundle.Remote!)
        {
            var path = bundle.Repositories[publisher];
            var payloads = new Dictionary<string, string>();
            foreach (
                var node in Files
                    .Xml(Path.Combine(path, "Updates.xml"))
                    .Root!.Elements("PackageUpdate")
            )
            {
                var id = node.Element("Name")!.Value;
                var version = node.Element("Version")!.Value;
                foreach (
                    var archive in (node.Element("DownloadableArchives")?.Value ?? "").Split(
                        ',',
                        StringSplitOptions.RemoveEmptyEntries
                    )
                )
                {
                    payloads[id + "/" + version + archive] = id;
                }
            }
            foreach (var (name, record) in remote!["files"]!.AsObject())
            {
                if (payloads.TryGetValue(name, out var id) && !selected.Contains(id))
                {
                    continue;
                }
                log("Verifying/download " + publisher + "/" + name);
                await VerifiedFile(
                    Files.Text(remote["url"]),
                    name,
                    record!,
                    cache,
                    Storage.Under(path, name),
                    source["allow_loopback_http"]?.GetValue<bool>() == true,
                    cacheOnly
                );
            }
        }
    }
}
