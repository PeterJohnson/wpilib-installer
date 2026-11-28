using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Layout;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using ManagerExperiment;
using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Text.Json.Nodes;

internal static class Program
{
    public static string? Smoke;
    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--engine-smoke") return Engine();
        if (args.Length > 0 && args[0] != "--ui-smoke") return Commands.Run(args).GetAwaiter().GetResult();
        Smoke = args.Length > 1 ? args[1] : null;
        return AppBuilder.Configure<ExperimentApp>().UsePlatformDetect().StartWithClassicDesktopLifetime(args);
    }
    public static JsonObject Bootstrap()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("bootstrap.json")!;
        using var bytes = new MemoryStream(); stream.CopyTo(bytes); return Storage.Object(bytes.ToArray());
    }
    static int Engine()
    {
        var directory = Directory.CreateTempSubdirectory("wpilib-csharp-worker-").FullName;
        try
        {
            foreach (var name in new[] { "engine.zip", "runtime.zip" })
            {
                using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
                if (stream is null) continue;
                using var zip = new ZipArchive(stream);
                var root = name == "runtime.zip" ? Path.Combine(directory, "runtime") : directory;
                foreach (var entry in zip.Entries)
                {
                    if (entry.FullName.EndsWith('/')) continue;
                    var path = Storage.Under(root, entry.FullName); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                    entry.ExtractToFile(path);
                    if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(path, (UnixFileMode)((entry.ExternalAttributes >> 16) & 511));
                }
            }
            var executable = OperatingSystem.IsWindows() ? "engine.exe" : OperatingSystem.IsMacOS() ? "engine.app/Contents/MacOS/engine" : "engine";
            if (OperatingSystem.IsMacOS()) executable = Path.GetRelativePath(directory, Directory.GetFiles(Path.Combine(directory, "engine.app/Contents/MacOS"))[0]);
            var command = new ProcessStartInfo(Path.Combine(directory, executable)) { UseShellExecute = false };
            command.ArgumentList.Add("--version");
            command.Environment["QT_QPA_PLATFORM"] = "offscreen";
            if (OperatingSystem.IsLinux())
            {
                command.Environment["LD_LIBRARY_PATH"] = Path.Combine(directory, "runtime/lib");
                command.Environment["FONTCONFIG_FILE"] = Path.Combine(directory, "runtime/share/fonts.conf");
                command.Environment["FONTCONFIG_PATH"] = Path.Combine(directory, "runtime/share");
            }
            using var process = Process.Start(command)!; process.WaitForExit(); return process.ExitCode;
        }
        finally { Directory.Delete(directory, true); }
    }
}
internal sealed class ExperimentApp : Application
{
    public override void Initialize() => Styles.Add(new FluentTheme());
    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            var status = new TextBlock { Text = "Experimental C# manager — authenticated catalog checks", TextWrapping = Avalonia.Media.TextWrapping.Wrap };
            var button = new Button { Content = "Check package catalogs" };
            button.Click += async (_, _) =>
            {
                button.IsEnabled = false;
                try
                {
                    var cache = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WPILib-CSharp-Experiment");
                    var result = await Commands.Registry(Program.Bootstrap(), cache, "live"); status.Text = result.ToJsonString();
                }
                catch (Exception e) { status.Text = e.Message; }
                finally { button.IsEnabled = true; }
            };
            var window = new Window { Title = "WPILib Manager — C# experiment", Width = 720, Height = 420,
                Content = new StackPanel { Margin = new Thickness(32), Spacing = 24, HorizontalAlignment = HorizontalAlignment.Stretch, Children = { status, button } } };
            desktop.MainWindow = window;
            if (Program.Smoke is not null)
            {
                window.Opened += (_, _) => DispatcherTimer.RunOnce(() => {
                    File.WriteAllText(Program.Smoke, new JsonObject { ["status"] = "ok", ["visible"] = window.IsVisible, ["width"] = window.Bounds.Width, ["height"] = window.Bounds.Height }.ToJsonString());
                    desktop.Shutdown();
                }, TimeSpan.FromSeconds(2));
            }
        }
        base.OnFrameworkInitializationCompleted();
    }
}
