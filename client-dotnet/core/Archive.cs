using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Math.EC.Rfc8032;
namespace Wpilib.Installer;
public static class Archive
{
    // Python json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=True).
    // TUF uses the library's distinct canonicalization; do not interchange them.
    public static byte[] Canonical(JsonNode value)
    {
        using var document = JsonDocument.Parse(value.ToJsonString());
        var output = new StringBuilder();
        void Quote(string s)
        {
            output.Append('"');
            foreach (char c in s)
                output.Append(c switch { '"' => "\\\"", '\\' => "\\\\", '\n' => "\\n", '\r' => "\\r", '\t' => "\\t", '\b' => "\\b", '\f' => "\\f",
                    < (char)32 or > (char)126 => "\\u" + ((int)c).ToString("x4"), _ => c.ToString() });
            output.Append('"');
        }
        void Encode(JsonElement e)
        {
            switch (e.ValueKind)
            {
                case JsonValueKind.Object:
                    output.Append('{'); bool first = true;
                    // Sort by Unicode scalar values, like Python (not UTF-16 ordinal).
                    foreach (var p in e.EnumerateObject().OrderBy(p => p.Name, Comparer<string>.Create(ScalarCompare)))
                    { if (!first) output.Append(','); first = false; Quote(p.Name); output.Append(':'); Encode(p.Value); }
                    output.Append('}'); break;
                case JsonValueKind.Array:
                    output.Append('['); first = true;
                    foreach (var child in e.EnumerateArray()) { if (!first) output.Append(','); first = false; Encode(child); }
                    output.Append(']'); break;
                case JsonValueKind.String: Quote(e.GetString()!); break;
                case JsonValueKind.Number:
                    if (e.TryGetInt64(out var signed)) output.Append(signed.ToString(System.Globalization.CultureInfo.InvariantCulture));
                    else if (e.TryGetUInt64(out var unsigned)) output.Append(unsigned.ToString(System.Globalization.CultureInfo.InvariantCulture));
                    else throw new IOException("noninteger archival number");
                    break;
                default: output.Append(e.GetRawText()); break;
            }
        }
        Encode(document.RootElement); return Encoding.ASCII.GetBytes(output.ToString());
    }
    static int ScalarCompare(string? a, string? b)
    {
        var x = a!.EnumerateRunes().Select(r => r.Value).ToArray(); var y = b!.EnumerateRunes().Select(r => r.Value).ToArray();
        for (int i = 0; i < Math.Min(x.Length, y.Length); i++) if (x[i] != y[i]) return x[i].CompareTo(y[i]);
        return x.Length.CompareTo(y.Length);
    }
    public static void Require(bool condition, string message) { if (!condition) throw new IOException(message); }
    public static JsonObject Signed(JsonObject envelope, string key)
    {
        Require(envelope.Count == 2 && envelope.ContainsKey("signed") && envelope.ContainsKey("signature"), "invalid envelope");
        var data = Canonical(envelope["signed"]!);
        var publicKey = new Ed25519PublicKeyParameters(Convert.FromBase64String(key));
        Require(publicKey.Verify(Ed25519.Algorithm.Ed25519, null, data, Convert.FromBase64String(envelope["signature"]!.GetValue<string>())), "invalid archival signature");
        return envelope["signed"]!.AsObject();
    }
    public static JsonObject Authorize(JsonObject source, string bundle)
    {
        var record = Storage.Object(Path.Combine(bundle, "registry.json"));
        var r = Signed(record, source["public_key"]!.GetValue<string>());
        var registry = r["registry"]!.AsObject();
        var policy = JsonNode.Parse("{\"name\":\"wpilib-archive-v2\",\"lifetime\":\"indefinite\"}");
        Require(JsonNode.DeepEquals(source["archive_policy"], policy) && JsonNode.DeepEquals(registry["archive_policy"], policy) && registry["online_trust"]!.GetValue<string>() == "tuf", "unsupported archival policy");
        Require(r["kind"]!.GetValue<string>() == "registry" && r["schema"]!.GetValue<int>() == 1 && JsonNode.DeepEquals(r["registry_id"], source["registry_id"]), "registry identity mismatch");
        foreach (var f in new[] { "season", "channel", "target" }) Require(JsonNode.DeepEquals(registry[f], source[f]), "scope mismatch");
        Require(r["revision"]!.GetValue<long>() > 0 && r["issued_at"]!.GetValue<long>() > 0 && r["expires_at"]!.GetValue<long>() > r["issued_at"]!.GetValue<long>(), "invalid revision/issuance");
        Require(r.ContainsKey("offline_expires_at") && r["offline_expires_at"] is null, "archival expiry");
        var manifest = Signed(Storage.Object(Path.Combine(bundle, "bundle-manifest.json")), registry["bundle_public_key"]!.GetValue<string>());
        var hash = Storage.Hash(Canonical(record));
        var identity = new JsonObject { ["registry_id"] = r["registry_id"]!.DeepClone(), ["revision"] = r["revision"]!.DeepClone(), ["sha256"] = hash };
        Require(manifest["schema"]!.GetValue<int>() == 3 && JsonNode.DeepEquals(manifest["registry"], identity), "bundle registry mismatch");
        var auth = manifest["archival"]!.AsObject();
        Require(auth["policy"]!.GetValue<string>() == "wpilib-archive-v2" && JsonNode.DeepEquals(auth["issued_at"], r["issued_at"]) && auth.ContainsKey("expires_at") && auth["expires_at"] is null && !auth.ContainsKey("not_before"), "invalid archival authorization");
        var repos = registry["repositories"]!.AsObject(); var pubs = auth["publishers_tuf"]!.AsObject();
        Require(repos.Count == pubs.Count, "publisher set mismatch");
        foreach (var (name, repo) in repos) Require(pubs.ContainsKey(name) && JsonNode.DeepEquals(pubs[name]!["repository_id"], repo!["tuf"]!["repository_id"]), "publisher identity mismatch");
        foreach (var e in pubs.Select(p => p.Value!).Append(auth["registry_tuf"]!))
        {
            Require(!string.IsNullOrEmpty(e["repository_id"]!.GetValue<string>()), "provenance identity");
            var h = e["catalog_sha256"]!.GetValue<string>(); Require(h.Length == 64 && h.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f'), "provenance hash");
        }
        return new JsonObject { ["status"] = "ok", ["registry_revision"] = r["revision"]!.DeepClone(), ["registry_sha256"] = hash };
    }
}
