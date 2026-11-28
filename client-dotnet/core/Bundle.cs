using System.Text.Json.Nodes;
namespace Wpilib.Installer;
public sealed class Bundle : IDisposable
{
    public Workspace Workspace { get; }
    public JsonObject Registry { get; }
    public JsonObject Identity { get; }
    public JsonObject Packages { get; } = new();
    public SortedDictionary<string,string> Repositories { get; } = new(StringComparer.Ordinal);
    public JsonObject? Remote { get; set; }
    Bundle(Workspace workspace,JsonObject registry,JsonObject identity) { Workspace=workspace; Registry=registry; Identity=identity; }
    public void Dispose() => Workspace.Dispose();
    public static Bundle Open(string path,JsonObject source,Action<string> log)
    {
        var workspace=new Workspace();
        try
        {
            log("Verifying offline bundle…");
            using(var stream=File.OpenRead(path)) Files.Extract(stream,workspace.Path,false);
            var members=Directory.GetFileSystemEntries(workspace.Path);
            Archive.Require(members.Length==1 && Directory.Exists(members[0]),"Expected one bundle directory");
            var root=members[0]; var identity=Archive.Authorize(source,root);
            var registry=Storage.Object(System.IO.Path.Combine(root,"registry.json"))["signed"]!["registry"]!.AsObject();
            var manifest=Storage.Object(System.IO.Path.Combine(root,"bundle-manifest.json"))["signed"]!;
            Scope(manifest,registry);
            return Load(workspace,root,registry,identity,manifest["repositories"]!.AsObject(),false,log);
        }
        catch { workspace.Dispose(); throw; }
    }
    public static void Scope(JsonNode value,JsonNode scope)
    { foreach(var field in new[]{"target","season","channel"}) Archive.Require(JsonNode.DeepEquals(value[field],scope[field]),"Scope mismatch: "+field); }
    public static Bundle Load(Workspace workspace,string root,JsonObject registry,JsonObject identity,JsonObject references,bool metadataOnly,Action<string> log)
    {
        var result=new Bundle(workspace,registry,identity); var policies=registry["repositories"]!.AsObject();
        Archive.Require(policies.Count==references.Count,"Publisher set mismatch");
        var allowedOwners=new HashSet<string>(StringComparer.Ordinal);
        foreach(var (publisher,policyNode) in policies.OrderBy(x=>x.Key,StringComparer.Ordinal))
        {
            Files.Safe(publisher); Archive.Require(!publisher.Contains('/'),"Invalid publisher");
            var policy=policyNode!.AsObject(); var allowed=policy["components"]!.AsArray().Select(Files.Text).ToHashSet(StringComparer.Ordinal);
            foreach(var id in allowed) Archive.Require(allowedOwners.Add(id),"Ambiguous component owner");
            var repo=System.IO.Path.Combine(root,"repos",publisher); var envelope=Storage.Object(System.IO.Path.Combine(repo,"release.json"));
            var release=Archive.Signed(envelope,Files.Text(policy["public_key"]));
            Archive.Require(release["schema"]!.GetValue<int>()==1 && Files.Text(release["publisher"])==publisher && JsonNode.DeepEquals(release["revision"],references[publisher]!["revision"]) && Storage.Hash(Archive.Canonical(envelope))==Files.Text(references[publisher]!["sha256"]),"Release identity mismatch");
            Archive.Require(release["revision"]!.GetValue<long>() >= (policy["minimum_revision"]?.GetValue<long>() ?? 1),"Release below revision floor"); Scope(release,registry);
            var files=release["files"]!.AsObject();
            foreach(var (name,record) in files)
            {
                Files.Safe(name); var hash=Files.Text(record!["sha256"]);
                Archive.Require(hash.Length==64 && hash.All(c=>char.IsAsciiDigit(c)||c is >= 'a' and <= 'f') && record["size"]!.GetValue<long>()>=0,"Invalid repository file record");
                if(!metadataOnly || name=="Updates.xml") Files.Verify(Storage.Under(repo,name),record);
            }
            var expected=new Dictionary<string,JsonNode>(StringComparer.Ordinal);
            foreach(var p in release["packages"]!.AsArray())
            {
                var id=Files.Text(p!["id"]);
                Archive.Require(id.Length>0 && id.All(c=>char.IsAsciiLetterLower(c)||char.IsAsciiDigit(c)||c=='.') && allowed.Contains(id) && !result.Packages.ContainsKey(id),"Unauthorized/duplicate component");
                Files.Version(Files.Text(p["version"])); result.Packages[id]=p.DeepClone(); expected.Add(id,p);
                var records=p["file_records"]!.AsObject(); var names=p["owned_files"]!.AsArray();
                Archive.Require(records.Count==names.Count,"Ownership records differ");
                foreach(var n in names)
                {
                    var name=Files.Text(n); Files.Safe(name); var top=name.Split('/')[0].ToLowerInvariant();
                    Archive.Require(!top.StartsWith(".wpilib") && top is not ("wpilib-manager" or "wpilibmanager.exe" or "wpilib manager.app" or "components.xml" or ".dat"),"Package owns manager state");
                    Archive.Require(records.ContainsKey(name),"Missing owned file record");
                }
            }
            var xml=Files.Xml(System.IO.Path.Combine(repo,"Updates.xml"));
            Archive.Require(!xml.Descendants().Any(n=>n.Name.LocalName is "RepositoryUpdate" or "RepositoryCategories"),"Repository redirects forbidden");
            var seen=new HashSet<string>();
            foreach(var node in xml.Root!.Elements("PackageUpdate"))
            {
                string Value(string name)=>node.Element(name)?.Value ?? "";
                var id=Value("Name"); Archive.Require(expected.ContainsKey(id) && seen.Add(id),"Unknown/duplicate Qt component"); var p=expected[id];
                Archive.Require(Value("Version")==Files.Text(p["version"]),"Qt version mismatch");
                var dependencies=p["dependencies"]?.AsObject().Select(d=>d.Key+"->="+Files.Text(d.Value)).ToHashSet() ?? [];
                Archive.Require(dependencies.SetEquals(Value("Dependencies").Split(',',StringSplitOptions.RemoveEmptyEntries)),"Qt dependency mismatch");
                foreach(var archive in Value("DownloadableArchives").Split(',',StringSplitOptions.RemoveEmptyEntries))
                    Archive.Require(files.ContainsKey(id+"/"+Files.Text(p["version"])+archive),"Unsigned Qt archive");
            }
            Archive.Require(seen.Count==expected.Count,"Incomplete Qt index"); result.Repositories.Add(publisher,repo);
            log("Verified "+publisher+(metadataOnly?" catalog and index":" repository files"));
        }
        ValidatePlan(result.Packages);
        foreach(var (_,profile) in registry["profiles"]!.AsObject()) foreach(var id in profile!.AsArray()) Archive.Require(result.Packages.ContainsKey(Files.Text(id)),"Profile requires absent package");
        return result;
    }
    public HashSet<string> Closure(IEnumerable<string> selected)
    {
        var result=new HashSet<string>(StringComparer.Ordinal); var todo=new Stack<string>(selected);
        while(todo.TryPop(out var id))
        {
            if(!result.Add(id)) continue;
            var p=Packages[id] ?? throw new IOException("Unknown requested component: "+id);
            foreach(var dep in p["dependencies"]?.AsObject().Select(x=>x.Key) ?? []) todo.Push(dep);
        }
        return result;
    }
    public static void ValidatePlan(JsonObject packages)
    {
        var owned=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(var (_,p) in packages)
        {
            foreach(var (id,minimum) in p!["dependencies"]?.AsObject() ?? new JsonObject())
            {
                var dependency=packages[id] ?? throw new IOException("Plan would remove a required dependency");
                Archive.Require(Files.Version(Files.Text(dependency["version"]))>=Files.Version(Files.Text(minimum)),"Plan does not satisfy dependencies");
            }
            foreach(var n in p["owned_files"]!.AsArray()) { var name=Files.Text(n); Files.Safe(name); Archive.Require(owned.Add(name),"Conflicting package ownership"); }
        }
        foreach(var name in owned)
        {
            var parent=name;
            while(parent.LastIndexOf('/') is var i && i>=0) { parent=parent[..i]; Archive.Require(!owned.Contains(parent),"File/directory ownership conflict"); }
        }
    }
}
