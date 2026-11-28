using TUF;
using System.Net;
var transport=new FixtureTransport(args[1],args[2],args[4]);
try
{
    var metadata=Path.Combine(args[2],"metadata");var targets=Path.Combine(args[2],"targets");Directory.CreateDirectory(metadata);Directory.CreateDirectory(targets);
    using var http=new HttpClient(new Handler(transport));
    var client=new Updater(new UpdaterConfig(File.ReadAllBytes(args[0]),new Uri("https://fixture.invalid/metadata/")) {
      RemoteTargetsUrl=new Uri("https://fixture.invalid/targets/"),LocalMetadataDir=metadata,LocalTargetsDir=targets,Client=http,MaxRootRotations=uint.Parse(args[3])});
    await client.RefreshAsync();
    var info=await client.GetTargetInfo("catalog.json") ?? throw new Exception("No catalog target");
    var result=await client.DownloadTarget(info.File,"catalog.json");
    return Output.Write(result.Data,null,transport.Requests);
}
catch(Exception e){return Output.Write(null,e,transport.Requests);}
sealed class Handler(FixtureTransport transport):HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken)
    {
      var data=transport.Read(request.RequestUri!.AbsolutePath.TrimStart('/'));
      return Task.FromResult(new HttpResponseMessage(data is null?HttpStatusCode.NotFound:HttpStatusCode.OK){Content=new ByteArrayContent(data??[])});
    }
}
