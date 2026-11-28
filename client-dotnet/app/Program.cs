using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using System.Diagnostics;
using System.Reflection;
using System.Text.Json.Nodes;
using Wpilib.Installer;

internal static class Program
{
    public static Request Request = new();
    [STAThread]
    public static int Main(string[] args)
    {
        Installation.Resource=name=>Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        try
        {
            // The native interoperability driver shares the installed verifier.
            if(args.Length>0&&!args[0].StartsWith("--",StringComparison.Ordinal))return Commands.Run(args).GetAwaiter().GetResult();
            Request=Request.Parse(args);
            if(Request.Command is not null)
            {
                var value=Installation.Execute(Request,Console.Error.WriteLine).GetAwaiter().GetResult();Console.WriteLine(value.ToJsonString());return 0;
            }
            var builder=AppBuilder.Configure<ManagerApp>().UsePlatformDetect();
            if(OperatingSystem.IsLinux() && !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY")))builder=builder.UseWayland();
            return builder.StartWithClassicDesktopLifetime(args);
        }
        catch(Exception e){Console.WriteLine(new JsonObject{["status"]="error",["message"]=e.Message}.ToJsonString());return 1;}
    }
}
internal sealed class ManagerApp : Application
{
    public override void Initialize()=>Styles.Add(new FluentTheme());
    public override void OnFrameworkInitializationCompleted()
    {
        if(ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)desktop.MainWindow=new ManagerWindow(Program.Request);
        base.OnFrameworkInitializationCompleted();
    }
}
internal sealed class ManagerWindow : Window
{
    readonly Request initial;
    readonly Stopwatch clock=Stopwatch.StartNew();
    readonly TextBox folder=new(),media=new();
    readonly ComboBox source=new(){ItemsSource=new[]{"Last installation source","Offline bundle","Online repositories","Verified online cache"},SelectedIndex=0};
    readonly ComboBox profile=new(){ItemsSource=new[]{"Individual packages"},SelectedIndex=0};
    readonly CheckBox licenses=new(){Content="I have reviewed and accept the selected packages’ licenses."};
    readonly StackPanel packageList=new(){Spacing=4};
    readonly TextBlock status=new(){Text="Choose a source and check the available packages.",TextWrapping=TextWrapping.Wrap};
    readonly TextBox logs=new(){IsReadOnly=true,AcceptsReturn=true,MinHeight=120,TextWrapping=TextWrapping.Wrap};
    readonly StackPanel inputs=new(){Spacing=12};
    readonly HashSet<string> selected;
    JsonObject? result;string? error;bool busy;bool rendered;
    public ManagerWindow(Request request)
    {
        initial=request;selected=new HashSet<string>(request.Packages);Title="WPILib Manager";Width=940;Height=780;MinWidth=740;MinHeight=640;
        folder.Text=request.Root;media.Text=request.Bundle??"";licenses.IsChecked=request.AcceptLicenses;
        source.SelectedIndex=request.Cached?3:request.Online?2:request.Bundle is not null?1:0;
        var browseFolder=new Button{Content="Browse folder…"};browseFolder.Click+=async(_,_)=>{var paths=await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions{AllowMultiple=false});if(paths.Count>0)folder.Text=paths[0].TryGetLocalPath();};
        var browseBundle=new Button{Content="Browse bundle…"};browseBundle.Click+=async(_,_)=>{var paths=await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions{AllowMultiple=false,FileTypeFilter=[new FilePickerFileType("Offline bundle"){Patterns=["*.zip"]}]});if(paths.Count>0){media.Text=paths[0].TryGetLocalPath();source.SelectedIndex=1;}};
        var actions=new WrapPanel{Orientation=Orientation.Horizontal};
        foreach(var (label,command) in new[]{("Check packages / updates","verify"),("Install selected / profile","install"),("Update selected (empty = all)","update"),("Remove selected","remove"),("Uninstall all packages","uninstall")})
        {
            var button=new Button{Content=label,Margin=new Thickness(0,0,8,8)};
            button.Click+=async(_,_)=>
            {
                if(command=="uninstall")
                {
                    var dialog=new Window{Title="Uninstall all packages?",Width=500,Height=220,WindowStartupLocation=WindowStartupLocation.CenterOwner};
                    var yes=new Button{Content="Uninstall packages"};var no=new Button{Content="Cancel"};yes.Click+=(_,_)=>dialog.Close(true);no.Click+=(_,_)=>dialog.Close(false);
                    dialog.Content=new StackPanel{Margin=new Thickness(24),Spacing=20,Children={new TextBlock{Text="Remove every installed package? Your own files and the maintenance application will remain.",TextWrapping=TextWrapping.Wrap},new StackPanel{Orientation=Orientation.Horizontal,Spacing=12,Children={yes,no}}}};
                    if(!await dialog.ShowDialog<bool>(this))return;
                }
                await Launch(command);
            };actions.Children.Add(button);
        }
        inputs.Children.Add(new TextBlock{Text="Installation folder"});inputs.Children.Add(Row(folder,browseFolder));
        inputs.Children.Add(new TextBlock{Text="Package source"});inputs.Children.Add(source);
        inputs.Children.Add(new TextBlock{Text="Offline bundle ZIP"});inputs.Children.Add(Row(media,browseBundle));
        inputs.Children.Add(new TextBlock{Text="Use case"});inputs.Children.Add(profile);
        inputs.Children.Add(new Expander{Header="Individual packages",IsExpanded=true,Content=new ScrollViewer{MaxHeight=170,Content=packageList}});
        inputs.Children.Add(licenses);inputs.Children.Add(actions);
        Content=new ScrollViewer{Content=new StackPanel{Margin=new Thickness(28),Spacing=16,Children={new TextBlock{Text="WPILib Manager",FontSize=28,FontWeight=FontWeight.SemiBold},new TextBlock{Text="Package installation and maintenance",FontSize=16},inputs,status,logs}}};
        Closing+=(_,e)=>{if(busy)e.Cancel=true;};
        Opened+=(_,_)=>DispatcherTimer.RunOnce(async()=>
        {
            rendered=true;
            if(initial.UiReport is null)return;
            Capture("startup");
            if(initial.UiCommand is not null)await Launch(initial.UiCommand);
            Capture("final");Close();
        },TimeSpan.FromMilliseconds(150));
    }
    static Grid Row(Control field,Control button)
    { var grid=new Grid{ColumnDefinitions=new ColumnDefinitions("*,Auto"),ColumnSpacing=12};Grid.SetColumn(button,1);grid.Children.Add(field);grid.Children.Add(button);return grid; }
    async Task Launch(string command)
    {
        if(busy)return;busy=true;inputs.IsEnabled=false;error=null;result=null;logs.Text="";status.Text="Working…";
        var req=new Request{Command=command,Root=folder.Text??"",Bundle=source.SelectedIndex==1 && !string.IsNullOrWhiteSpace(media.Text)?media.Text:null,Online=source.SelectedIndex==2,Cached=source.SelectedIndex==3,Packages=selected.ToList(),AcceptLicenses=licenses.IsChecked==true,Profile=command=="install"?(initial.UiReport is not null?initial.Profile:profile.SelectedIndex>0?profile.SelectedItem as string:null):null};
        try
        {
            result=await Task.Run(()=>Installation.Execute(req,line=>Dispatcher.UIThread.Post(()=>{logs.Text=(logs.Text+line+Environment.NewLine);if(logs.Text.Length>30000)logs.Text=logs.Text[^25000..];})));
            status.Text="Operation completed successfully.";
            if(result["packages"] is JsonObject packages)
            {
                packageList.Children.Clear();
                foreach(var (id,p) in packages)
                {
                    var box=new CheckBox{Content=(p!["name"]?.GetValue<string>()??id)+" "+Files.Text(p["version"])+" — "+(result["inventory"]![id]?.GetValue<string>()??"not installed"),IsChecked=selected.Contains(id)};
                    box.IsCheckedChanged+=(_,_)=>{if(box.IsChecked==true)selected.Add(id);else selected.Remove(id);};packageList.Children.Add(box);
                }
                profile.ItemsSource=new[]{"Individual packages"}.Concat(result["profiles"]!.AsObject().Select(x=>x.Key)).ToArray();profile.SelectedIndex=0;
                status.Text=$"Verified package catalog. {result["updates"]!.AsArray().Count} update(s) available.";
            }
        }
        catch(Exception e){error=e.Message;status.Text="Unable to complete: "+error;}
        finally{busy=false;inputs.IsEnabled=true;}
    }
    void Capture(string phase)
    {
        var report=Path.GetFullPath(initial.UiReport!);Directory.CreateDirectory(Path.GetDirectoryName(report)!);var png=Path.ChangeExtension(report,phase+".png");
        var size=new PixelSize((int)Bounds.Width,(int)Bounds.Height);using var bitmap=new RenderTargetBitmap(size,new Vector(96,96));bitmap.Render(this);bitmap.Save(png,PngBitmapEncoderOptions.Default);
        var value=new JsonObject{["native_window"]=rendered&&IsVisible&&TryGetPlatformHandle() is not null,["phase"]=phase,["elapsed_ms"]=clock.ElapsedMilliseconds,["size"]=new JsonArray(size.Width,size.Height),["screenshot"]=png,["result"]=result?.DeepClone(),["error"]=error};
        Storage.Write(phase=="startup"?Path.ChangeExtension(report,"startup.json"):report,Archive.Canonical(value));
    }
}
