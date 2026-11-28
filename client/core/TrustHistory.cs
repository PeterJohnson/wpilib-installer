using System.Text;
using System.Text.Json.Nodes;
using Tuf;
using Tuf.Metadata;

namespace Wpilib.Installer;

// Historical proof authentication only. No result of this class is a usable
// online/cache view: TufClient must still enforce the current clock for that.
public static class TrustHistory
{
    static byte[] Canonical(JsonNode? node)
    {
        var b = new StringBuilder();
        void Quote(string text) =>
            b.Append('"').Append(text.Replace("\\", "\\\\").Replace("\"", "\\\"")).Append('"');
        void Write(JsonNode? n)
        {
            if (n is JsonObject obj)
            {
                b.Append('{');
                bool first = true;
                foreach (var (k, v) in obj.OrderBy(x => x.Key, StringComparer.Ordinal))
                {
                    if (!first)
                    {
                        b.Append(',');
                    }
                    first = false;
                    Quote(k);
                    b.Append(':');
                    Write(v);
                }
                b.Append('}');
            }
            else if (n is JsonArray array)
            {
                b.Append('[');
                for (int i = 0; i < array.Count; i++)
                {
                    if (i > 0)
                    {
                        b.Append(',');
                    }
                    Write(array[i]);
                }
                b.Append(']');
            }
            else if (n is JsonValue value && value.TryGetValue<string>(out var text))
            {
                Quote(text);
            }
            else
            {
                var raw = n?.ToJsonString() ?? "null";
                Archive.Require(
                    !raw.Contains('.') && !raw.Contains('e') || raw is "true" or "false",
                    "Noninteger TUF number"
                );
                b.Append(raw);
            }
        }
        Write(node);
        return Encoding.UTF8.GetBytes(b.ToString());
    }

    public static JsonObject RoleKeys(JsonObject root, string role)
    {
        var signed = root["signed"]!;
        var result = new JsonObject();
        foreach (var id in signed["roles"]![role]!["keyids"]!.AsArray().Select(Files.Text))
        {
            result[id] = signed["keys"]![id]!.DeepClone();
        }
        return result;
    }

    public static void Verify(JsonObject envelope, JsonObject root, string role)
    {
        var r = root["signed"]!;
        var definition = r["roles"]![role]!;
        var keys = r["keys"]!
            .AsObject()
            .ToDictionary(
                x => x.Key,
                x => new TufKey
                {
                    KeyType = Files.Text(x.Value!["keytype"]),
                    Scheme = Files.Text(x.Value["scheme"]),
                    KeyVal = x.Value["keyval"]!
                        .AsObject()
                        .ToDictionary(k => k.Key, k => Files.Text(k.Value)),
                }
            );
        var signatures = envelope["signatures"]!
            .AsArray()
            .Select(s => new TufSignature
            {
                KeyId = Files.Text(s!["keyid"]),
                Sig = Files.Text(s["sig"]),
            })
            .ToList();
        var tufRole = new TufRole
        {
            KeyIds = definition["keyids"]!.AsArray().Select(Files.Text).ToList(),
            Threshold = definition["threshold"]!.GetValue<int>(),
        };
        Archive.Require(
            Files.Text(envelope["signed"]!["_type"]) == role
                && TufMetadataVerifier.VerifyThreshold(
                    signatures,
                    Canonical(envelope["signed"]),
                    tufRole,
                    keys
                ),
            "Invalid historical " + role + " signature"
        );
    }

    public static JsonObject Root(JsonNode pin, string roots, int end)
    {
        var root = pin.DeepClone().AsObject();
        Verify(root, root, "root");
        var initial = root["signed"]!["version"]!.GetValue<int>();
        Archive.Require(end >= initial, "Invalid root checkpoint range");
        for (long v = (long)initial + 1; v <= end; v++)
        {
            var next = Storage.Object(Path.Combine(roots, v + ".root.json"));
            Archive.Require(
                next["signed"]!["version"]!.GetValue<int>() == v,
                "Root history version mismatch"
            );
            Verify(next, root, "root");
            Verify(next, next, "root");
            root = next;
        }
        return root;
    }

    public static void Checkpoint(JsonNode pin, string roots, JsonObject checkpoint)
    {
        var root = Root(
            pin,
            roots,
            checkpoint["root.json"]!["signed"]!["version"]!.GetValue<int>()
        );
        Archive.Require(
            JsonNode.DeepEquals(root, checkpoint["root.json"]),
            "Checkpoint differs from authenticated root history"
        );
        foreach (var role in new[] { "timestamp", "snapshot", "targets" })
        {
            if (checkpoint[role + ".json"] is JsonObject metadata)
            {
                Verify(metadata, root, role);
            }
        }
    }

    static void Link(byte[] data, JsonNode record, bool target = false)
    {
        if (record["length"] is not null)
        {
            Archive.Require(
                data.LongLength == record["length"]!.GetValue<long>(),
                "Historical length mismatch"
            );
        }
        if (!target)
        {
            Archive.Require(
                Storage.Object(data)["signed"]!["version"]!.GetValue<int>()
                    == record["version"]!.GetValue<int>(),
                "Historical version mismatch"
            );
        }
        if (record["hashes"] is JsonObject hashes)
        {
            Archive.Require(hashes.ContainsKey("sha256"), "SHA256 proof required");
            Archive.Require(
                Storage.Hash(data) == Files.Text(hashes["sha256"]),
                "Historical hash mismatch"
            );
        }
        else
        {
            Archive.Require(!target, "Target hash required");
        }
    }

    public static JsonNode Catalog(
        JsonNode pin,
        string roots,
        string proof,
        int rootVersion,
        string? expectedHash = null
    )
    {
        var root = Root(pin, roots, rootVersion);
        var timestamp = Storage.Object(Path.Combine(proof, "metadata/timestamp.json"));
        Verify(timestamp, root, "timestamp");
        var snapref = timestamp["signed"]!["meta"]!["snapshot.json"]!;
        var snapshot = Storage.Read(
            Path.Combine(proof, $"metadata/{snapref["version"]}.snapshot.json")
        );
        Link(snapshot, snapref);
        var snap = Storage.Object(snapshot);
        Verify(snap, root, "snapshot");
        var targetref = snap["signed"]!["meta"]!["targets.json"]!;
        var targets = Storage.Read(
            Path.Combine(proof, $"metadata/{targetref["version"]}.targets.json")
        );
        Link(targets, targetref);
        var target = Storage.Object(targets);
        Verify(target, root, "targets");
        var catalogref = target["signed"]!["targets"]!["catalog.json"]!;
        var data = Storage.Read(
            Path.Combine(
                proof,
                "targets/" + Files.Text(catalogref["hashes"]!["sha256"]) + ".catalog.json"
            )
        );
        Link(data, catalogref, true);
        if (expectedHash is not null)
        {
            Archive.Require(Storage.Hash(data) == expectedHash, "Cache catalog identity mismatch");
        }
        return JsonNode.Parse(data)!;
    }
}
