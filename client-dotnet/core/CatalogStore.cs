using System.Net;
using System.Text.Json.Nodes;
namespace Wpilib.Installer;
public sealed record VerifiedCatalog(JsonNode Value,JsonNode? Previous,string Status,int RootVersion,string Directory,JsonObject Head)
{
    public void Accept() => Storage.Write(Path.Combine(Directory,"accepted.json"),Archive.Canonical(Head));
}
public static class CatalogStore
{
    public static Uri Url(string value,bool development)
    {
        var u=new Uri(value,UriKind.Absolute);
        Archive.Require(u.Host.Length>0 && u.UserInfo.Length==0 && u.Query.Length==0 && u.Fragment.Length==0 &&
            (u.Scheme=="https" || development && u.Scheme=="http" && u.Host is "127.0.0.1" or "[::1]"),"Repository requires HTTPS");return u;
    }
    static IEnumerable<string> Walk(string directory)
    {
        Files.RejectLink(directory);
        foreach(var path in System.IO.Directory.EnumerateFileSystemEntries(directory).Order(StringComparer.Ordinal))
        {
            Files.RejectLink(path);
            if(System.IO.Directory.Exists(path)) { foreach(var file in Walk(path))yield return file; }
            else yield return path;
        }
    }
    static JsonObject FileHashes(string directory)
    {
        var files=new JsonObject();
        foreach(var file in Walk(directory))files[Path.GetRelativePath(directory,file).Replace('\\','/')]=Files.Hash(file);
        return files;
    }
    static JsonNode Historical(JsonNode pin,string roots,string directory,JsonObject head)
    {
        var catalog=TrustHistory.Catalog(pin,roots,Proof(directory,head),head["root_version"]!.GetValue<int>());
        Archive.Require(Storage.Hash(Archive.Canonical(catalog))==Files.Text(head["generation"]),"Cache catalog identity mismatch");return catalog;
    }
    static string Proof(string directory,JsonObject head)
    {
        var generation=Files.Text(head["generation"]);var proof=Files.Text(head["proof"]);
        foreach(var h in new[]{generation,proof}) Archive.Require(h.Length==64 && h.All(c=>char.IsAsciiDigit(c)||c is >= 'a' and <= 'f'),"Invalid cache proof identity");
        var path=Path.Combine(directory,"generations",generation,proof);
        Archive.Require(Storage.Hash(Archive.Canonical(FileHashes(path)))==proof,"Corrupt verified metadata cache");return path;
    }
    static void CopyTree(string from,string to)
    {
        foreach(var file in Walk(from))
        { Files.RejectLink(file);var dest=Path.Combine(to,Path.GetRelativePath(from,file));System.IO.Directory.CreateDirectory(Path.GetDirectoryName(dest)!);File.Copy(file,dest,true); }
    }
    static bool Unavailable(Exception error) => error is TaskCanceledException || error is HttpRequestException h &&
        (h.StatusCode is >= HttpStatusCode.InternalServerError || h.HttpRequestError is HttpRequestError.ConnectionError or HttpRequestError.NameResolutionError);
    public static async Task<VerifiedCatalog> Load(JsonNode trust,string cache,bool development,bool cacheOnly,Action<string> log)
    {
        var id=Files.Text(trust["repository_id"]);Archive.Require(id.Length is >0 and <=160 && id.All(c=>char.IsAsciiLetterOrDigit(c)||c is '_' or '-'),"Invalid TUF repository identity");
        Archive.Require(trust["root"]!["signed"]!["consistent_snapshot"]!.GetValue<bool>(),"Consistent snapshots required");
        var identity=new JsonObject { ["id"]=id,["root"]=trust["root"]!.DeepClone() };
        var directory=Path.Combine(cache,"tuf",Storage.Hash(Archive.Canonical(identity)));System.IO.Directory.CreateDirectory(directory);
        using var gate=new FileStream(Path.Combine(directory,".operation-lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);
        var roots=Path.Combine(directory,"roots");System.IO.Directory.CreateDirectory(roots);
        var pin=trust["root"]!;var headPath=Path.Combine(directory,"current.json");var checkpointPath=Path.Combine(directory,"checkpoint.json");
        var head=File.Exists(headPath)?Storage.Object(headPath):null;
        var checkpoint=File.Exists(checkpointPath)?Storage.Object(checkpointPath):new JsonObject { ["root.json"]=pin.DeepClone() };
        Archive.Require(head is null || File.Exists(checkpointPath),"Missing authenticated checkpoint");TrustHistory.Checkpoint(pin,roots,checkpoint);
        JsonNode? previous=null;
        if(head is not null) Historical(pin,roots,directory,head);
        var acceptedPath=Path.Combine(directory,"accepted.json");
        if(File.Exists(acceptedPath)) { Archive.Require(head is not null,"Missing high-water view");var accepted=Storage.Object(acceptedPath);previous=Historical(pin,roots,directory,accepted); }
        var origin=Url(Files.Text(trust["url"]),development).AbsoluteUri;bool cached=cacheOnly;
        for(int attempt=0;attempt<2;attempt++)
        {
            using var work=new Workspace();var metadata=Path.Combine(work.Path,"metadata");System.IO.Directory.CreateDirectory(metadata);
            var responses=Path.Combine(work.Path,"responses");System.IO.Directory.CreateDirectory(responses);
            if(head is not null) CopyTree(Proof(directory,head),responses);
            var cachedRoots=Path.Combine(responses,"metadata");System.IO.Directory.CreateDirectory(cachedRoots);
            foreach(var file in System.IO.Directory.EnumerateFiles(roots,"*.root.json")) File.Copy(file,Path.Combine(cachedRoots,Path.GetFileName(file)),true);
            var floors=new JsonObject();
            foreach(var (name,role) in checkpoint)
            {
                if(role is null)continue; var nameWithoutExtension=Path.GetFileNameWithoutExtension(name);
                floors[nameWithoutExtension]=role["signed"]!["version"]!.DeepClone();Storage.Write(Path.Combine(metadata,name),Archive.Canonical(role));
            }
            Storage.Write(Path.Combine(work.Path,"floors.json"),Archive.Canonical(floors));
            if(cached) Archive.Require(head is not null && JsonNode.DeepEquals(head["root_version"],checkpoint["root.json"]!["signed"]!["version"]),"Cached view predates verified root progress");
            JsonObject? result=null;Exception? failure=null;
            try { using var online=new Online(Archive.Canonical(pin),origin,work.Path,cached?"cache":"live",1024);result=await online.Fetch(); }
            catch(Exception e) { failure=e; }
            // Persist only library-accepted roots/metadata, including partial refresh floors.
            foreach(var file in System.IO.Directory.EnumerateFiles(cachedRoots,"*.root.json")) Storage.Write(Path.Combine(roots,Path.GetFileName(file)),Storage.Read(file));
            var next=new JsonObject { ["root.json"]=File.Exists(Path.Combine(metadata,"root.json"))?Storage.Object(Path.Combine(metadata,"root.json")):checkpoint["root.json"]!.DeepClone() };
            foreach(var role in new[]{"timestamp","snapshot","targets"})
            {
                var path=Path.Combine(metadata,role+".json");if(!File.Exists(path))continue;var value=Storage.Object(path);
                try { TrustHistory.Verify(value,next["root.json"]!.AsObject(),role);next[role+".json"]=value; }
                catch(IOException) { /* Role-key rotation invalidates the previous role's checkpoint. */ }
            }
            TrustHistory.Checkpoint(pin,roots,next);
            Archive.Require(next["root.json"]!["signed"]!["version"]!.GetValue<int>()>=checkpoint["root.json"]!["signed"]!["version"]!.GetValue<int>(),"Root checkpoint rollback");
            checkpoint=next;Storage.Write(checkpointPath,Archive.Canonical(checkpoint));
            if(failure is not null)
            {
                if(!cached && head is not null && Unavailable(failure)) { cached=true;log(id+": transport unavailable; revalidating cached metadata at the current clock");continue; }
                throw failure;
            }
            var catalog=result!["catalog"]!;JsonObject newHead;
            if(cached)newHead=head!;
            else
            {
                // Root evidence has its own authenticated history; keep generation proofs small.
                foreach(var file in System.IO.Directory.EnumerateFiles(cachedRoots,"*.root.json"))File.Delete(file);
                var files=FileHashes(responses);var proof=Storage.Hash(Archive.Canonical(files));
                var generation=Storage.Hash(Archive.Canonical(catalog));
                var dest=Path.Combine(directory,"generations",generation,proof);System.IO.Directory.CreateDirectory(dest);CopyTree(responses,dest);
                newHead=new JsonObject { ["generation"]=generation,["proof"]=proof,["root_version"]=result["root_version"]!.DeepClone() };Storage.Write(headPath,Archive.Canonical(newHead));
            }
            return new VerifiedCatalog(catalog.DeepClone(),previous,cached?"verified-cache":"verified-online",result["root_version"]!.GetValue<int>(),directory,newHead);
        }
        throw new IOException("No verified cache available");
    }
}
