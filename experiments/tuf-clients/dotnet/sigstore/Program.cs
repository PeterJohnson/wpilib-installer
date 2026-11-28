using Tuf;
using System.Text.Json;
var transport=new FixtureTransport(args[1],args[2],args[4]);
try
{
    using var client=new TufClient(new TufClientOptions {
        MetadataBaseUrl=new Uri("https://fixture.invalid/metadata/"),TargetsBaseUrl=new Uri("https://fixture.invalid/targets/"),
        TrustedRoot=File.ReadAllBytes(args[0]),MaxRootRotations=int.Parse(args[3]),
        Cache=new FileSystemTufCache(Path.Combine(args[2],"metadata")),Repository=new Repository(transport)});
    var result=await client.GetTargetAsync("catalog.json");
    return Output.Write(result.Content.ToArray(),null,transport.Requests);
}
catch(Exception e){return Output.Write(null,e,transport.Requests);}
sealed class Repository(FixtureTransport transport):ITufRepository
{
    public Task<byte[]?> FetchMetadataAsync(string role,int? version=null,CancellationToken cancellationToken=default)
      =>Task.FromResult(transport.Read("metadata/"+(version is null?"":version+".")+role+".json"));
    public Task<byte[]?> FetchTargetAsync(string targetPath,CancellationToken cancellationToken=default)
      =>Task.FromResult(transport.Read("targets/"+targetPath));
}
