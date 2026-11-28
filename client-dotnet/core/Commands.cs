using System.Text.Json.Nodes;
namespace Wpilib.Installer;

public static class Commands
{
    public static async Task<int> Run(string[] args)
    {
        try
        {
            JsonObject result;
            if (args[0] == "registry")
                result = await Registry(Storage.Object(args[1]), args[2], args.Length > 3 ? args[3] : "live");
            else if (args[3] == "archive")
                result = Archive.Authorize(Storage.Object(args[0]), args[1]);
            else
            {
                using var online = new Online(Storage.Read(args[0]), args[1], args[2], args[3], int.Parse(args[5]));
                result = await online.Fetch(int.Parse(args[4]));
            }
            Console.WriteLine(result.ToJsonString());
            return 0;
        }
        catch (Exception e)
        {
            Console.WriteLine(new JsonObject { ["status"] = "error", ["error"] = e.Message }.ToJsonString());
            return 1;
        }
    }
    public static async Task<JsonObject> Registry(JsonObject source, string cache, string mode)
    {
        async Task<JsonNode> Fetch(JsonNode trust)
        {
            var id = new JsonObject { ["repository_id"] = trust["repository_id"]!.DeepClone(), ["root"] = trust["root"]!.DeepClone() };
            using var online = new Online(Archive.Canonical(trust["root"]!), trust["url"]!.GetValue<string>(), Path.Combine(cache, Storage.Hash(Archive.Canonical(id))), mode, 256);
            return (await online.Fetch())["catalog"]!.DeepClone();
        }
        var identity = new JsonObject();
        foreach (var field in new[] { "registry_id", "season", "channel", "target", "tuf" })
            identity[field] = source[field]!.DeepClone();
        // Endpoint moves must not reset the application revision floor.
        identity["tuf"]!.AsObject().Remove("url");
        var checkpoint = Path.Combine(cache, "registry-" + Storage.Hash(Archive.Canonical(identity)) + ".json");
        Directory.CreateDirectory(cache);
        using var gate = new FileStream(checkpoint + ".lock", FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        var record = await Fetch(source["tuf"]!); // Online authenticity comes solely from TUF, not archival signatures.
        var signed = record["signed"]!;
        var registry = signed["registry"]!;
        Archive.Require(signed["kind"]!.GetValue<string>() == "registry" && signed["schema"]!.GetValue<int>() == 1 && JsonNode.DeepEquals(signed["registry_id"], source["registry_id"]), "registry identity mismatch");
        foreach (var field in new[] { "season", "channel", "target" })
            Archive.Require(JsonNode.DeepEquals(registry[field], source[field]), "registry scope mismatch");
        var revision = signed["revision"]!.GetValue<long>();
        Archive.Require(revision >= (source["minimum_revision"]?.GetValue<long>() ?? 1), "registry revision below bootstrap floor");
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        Archive.Require(registry["online_trust"]!.GetValue<string>() == "tuf" && signed["issued_at"]!.GetValue<long>() > 0 && signed["issued_at"]!.GetValue<long>() <= now && signed["expires_at"]!.GetValue<long>() > now, "registry online authorization expired/not yet valid");
        var recordHash = Storage.Hash(Archive.Canonical(record));
        if (File.Exists(checkpoint))
        {
            var old = Storage.Object(checkpoint);
            var prior = old["revision"]!.GetValue<long>();
            Archive.Require(revision > prior || revision == prior && recordHash == old["sha256"]!.GetValue<string>(), "registry rollback or same-revision mutation");
        }
        Storage.Write(checkpoint, Archive.Canonical(new JsonObject { ["revision"] = revision, ["sha256"] = recordHash }));
        var pubs = new JsonObject();
        var ids = new HashSet<string> { source["tuf"]!["repository_id"]!.GetValue<string>() };
        var repos = registry["repositories"]!.AsObject();
        Archive.Require(registry["schema"]!.GetValue<int>() == 1 && repos.Count is > 0 and <= 64, "invalid registry schema/publisher count");
        foreach (var (name, repo) in repos)
        {
            Archive.Require(ids.Add(repo!["tuf"]!["repository_id"]!.GetValue<string>()), "publisher TUF identities must be independent");
            var catalog = await Fetch(repo["tuf"]!);
            Archive.Require(catalog["kind"]!.GetValue<string>() == "publisher-catalog" && catalog["schema"]!.GetValue<int>() == 1 && catalog["publisher"]!.GetValue<string>() == name, "publisher identity mismatch");
            foreach (var field in new[] { "season", "channel", "target" })
                Archive.Require(JsonNode.DeepEquals(catalog[field], source[field]), "publisher scope mismatch");
            pubs[name] = catalog;
        }
        return new JsonObject { ["status"] = "ok", ["registry_revision"] = revision, ["publishers"] = pubs };
    }
}
