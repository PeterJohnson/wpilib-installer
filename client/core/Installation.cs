using System.Diagnostics;
using System.Text.Json.Nodes;

namespace Wpilib.Installer;

public sealed class Request
{
    public string? Command { get; set; }
    public string Root { get; set; } = Installation.DefaultRoot();
    public string? Bundle { get; set; }
    public List<string> Packages { get; set; } = [];
    public string? Profile { get; set; }
    public bool Online { get; set; }
    public bool Cached { get; set; }
    public bool AcceptLicenses { get; set; }
    public string? UiReport { get; set; }
    public string? UiCommand { get; set; }

    public static Request Parse(string[] args)
    {
        var r = new Request();
        for (int i = 0; i < args.Length; i++)
        {
            string Value() =>
                ++i < args.Length ? args[i] : throw new IOException("Missing argument value");
            switch (args[i])
            {
                case "--command":
                    r.Command = Value();
                    break;
                case "--root":
                    r.Root = Value();
                    break;
                case "--bundle":
                    r.Bundle = Value();
                    break;
                case "--package":
                    r.Packages.Add(Value());
                    break;
                case "--profile":
                    r.Profile = Value();
                    break;
                case "--online":
                    r.Online = true;
                    break;
                case "--cached":
                    r.Cached = true;
                    break;
                case "--accept-licenses":
                    r.AcceptLicenses = true;
                    break;
                case "--ui-test-report":
                    r.UiReport = Value();
                    break;
                case "--ui-test-command":
                    r.UiCommand = Value();
                    break;
                default:
                    throw new IOException("Unknown argument: " + args[i]);
            }
        }
        return r;
    }
}

public static class Installation
{
    public static Func<string, Stream?> Resource { get; set; } =
        _ => throw new IOException("Missing compiled assets");

    public static JsonObject Source()
    {
        using var stream = Resource("bootstrap.json")!;
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return Storage.Object(buffer.ToArray());
    }

    public static string Target =>
        (
            OperatingSystem.IsWindows() ? "windows"
            : OperatingSystem.IsMacOS() ? "macos"
            : "linux"
        )
        + "-"
        + (
            System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture
            == System.Runtime.InteropServices.Architecture.Arm64
                ? "arm64"
                : "x64"
        );
    public static string PublicApplication =>
        Environment.GetEnvironmentVariable("WPILIB_MANAGER_APPLICATION")
        ?? throw new IOException("Launch the public manager application");

    public static string AppPath(string root) =>
        Path.Combine(
            root,
            OperatingSystem.IsWindows() ? "WPILibManager.exe"
                : OperatingSystem.IsMacOS() ? "WPILib Manager.app/Contents/MacOS/wpilib-manager"
                : "wpilib-manager"
        );

    public static string EnginePath(string root) =>
        Path.Combine(
            root,
            OperatingSystem.IsWindows() ? ".wpilib-engine.exe"
                : OperatingSystem.IsMacOS() ? ".wpilib-engine.app"
                : ".wpilib-engine"
        );

    public static string DefaultRoot()
    {
        var application = Environment.GetEnvironmentVariable("WPILIB_MANAGER_APPLICATION");
        if (application is not null)
        {
            var parent = Path.GetDirectoryName(application)!;
            if (OperatingSystem.IsMacOS())
            {
                for (int i = 0; i < 3; i++)
                {
                    parent = Path.GetDirectoryName(parent)!;
                }
            }
            if (File.Exists(Path.Combine(parent, ".wpilib-manager.json")))
            {
                return parent;
            }
        }
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "wpilib-packages"
        );
    }

    static JsonObject Binding(JsonObject source)
    {
        var value = new JsonObject();
        foreach (var f in new[] { "registry_id", "season", "channel", "target" })
        {
            value[f] = source[f]!.DeepClone();
        }
        value["archive_key"] = source["public_key"]!.DeepClone();
        return value;
    }

    public static async Task<JsonObject> Execute(Request request, Action<string> log)
    {
        var command = request.Command ?? throw new IOException("Choose an operation");
        var source = Source();
        Archive.Require(
            Files.Text(source["target"]) == Target,
            "Bootstrap target differs from host"
        );
        var root = Path.GetFullPath(request.Root);
        var parent = Path.GetDirectoryName(root)
            ?? throw new IOException("Invalid installation root");
        Directory.CreateDirectory(parent);
        Archive.Require(Path.GetFileName(root).Length > 0, "Invalid installation root");
        using var gate = new FileStream(
            Path.Combine(parent, "." + Path.GetFileName(root) + ".manager-lock"),
            FileMode.OpenOrCreate,
            FileAccess.ReadWrite,
            FileShare.None
        );
        var statePath = Path.Combine(root, ".wpilib-manager.json");
        var state = File.Exists(statePath) ? Storage.Object(statePath) : null;
        if (state is not null)
        {
            Archive.Require(
                JsonNode.DeepEquals(state["binding"], Binding(source)),
                "Installation belongs to a different bootstrap trust/scope"
            );
        }
        // Qt owns the installed inventory. The manager state records trust and package
        // provenance, but cannot substitute for what the worker actually installed.
        var installed = Files.Inventory(root);
        Archive.Require(
            installed.Count == 0 || state is not null,
            "Unmanaged Qt installation; migration required"
        );
        if (command == "list")
        {
            return new JsonObject
            {
                ["status"] = "ok",
                ["inventory"] = Files.Json(installed),
                ["installation"] = root,
            };
        }
        Archive.Require(
            command
                is "verify"
                    or "check-updates"
                    or "install"
                    or "update"
                    or "remove"
                    or "uninstall",
            "Unsupported command"
        );
        Archive.Require(
            !(request.Online || request.Cached) || request.Bundle is null,
            "Choose online or offline media"
        );
        if (command == "uninstall")
        {
            return await Uninstall(root, state, installed, request.AcceptLicenses, log);
        }
        bool online =
            request.Online
            || request.Cached
            || request.Bundle is null && state?["last_source"]?.GetValue<string>() == "online";
        var onlineCache = Path.Combine(parent, "." + Path.GetFileName(root) + ".online");
        using var work = new Workspace();
        string? hash = null,
            snapshot = null;
        Bundle bundle;
        if (online)
        {
            bundle = await OnlinePackages.Load(source, onlineCache, request.Cached, log);
        }
        else
        {
            var path = request.Bundle;
            if (path is null)
            {
                Archive.Require(state is not null, "Choose an offline bundle ZIP");
                var oldHash = Files.Text(state!["bundle_sha256"]);
                Archive.Require(
                    oldHash.Length == 64 && oldHash.All(char.IsAsciiHexDigit),
                    "Invalid cached bundle identity"
                );
                path = Path.Combine(root, ".wpilib-manager", oldHash + ".zip");
            }
            snapshot = Path.Combine(work.Path, "bundle.zip");
            File.Copy(path, snapshot);
            hash = Files.Hash(snapshot);
            if (request.Bundle is null)
            {
                Archive.Require(
                    hash == Files.Text(state!["bundle_sha256"]),
                    "Cached bundle digest mismatch"
                );
            }
            bundle = Bundle.Open(snapshot, source, log);
        }
        using (bundle)
        {
            var updates = new JsonArray();
            foreach (var (id, version) in installed)
            {
                if (
                    bundle.Packages[id] is JsonNode p
                    && Files.Version(Files.Text(p["version"])) > Files.Version(version)
                )
                {
                    updates.Add(
                        (JsonNode)
                            new JsonObject
                            {
                                ["id"] = id,
                                ["installed"] = version,
                                ["available"] = p["version"]!.DeepClone(),
                            }
                    );
                }
            }
            if (command is "verify" or "check-updates")
            {
                return new JsonObject
                {
                    ["protocol_version"] = 1,
                    ["status"] = "ok",
                    ["installation"] = root,
                    ["inventory"] = Files.Json(installed),
                    ["updates"] = updates,
                    ["packages"] = bundle.Packages.DeepClone(),
                    ["profiles"] = bundle.Registry["profiles"]!.DeepClone(),
                    ["registry"] = bundle.Identity.DeepClone(),
                    ["bundle_sha256"] = hash,
                };
            }
            Archive.Require(
                request.AcceptLicenses,
                "Review and explicitly accept package licenses before applying changes"
            );
            Archive.Require(
                state is not null || command == "install",
                "Install packages before maintenance"
            );
            Archive.Require(
                state is not null
                    || !Directory.Exists(root)
                    || !Directory.EnumerateFileSystemEntries(root).Any(),
                "Fresh installation needs an empty directory"
            );
            var selected = new HashSet<string>(request.Packages, StringComparer.Ordinal);
            if (request.Profile is not null)
            {
                Archive.Require(command == "install", "Profiles are only valid for install");
                var profile = bundle.Registry["profiles"]![request.Profile]?.AsArray()
                    ?? throw new IOException("Unknown profile");
                selected.UnionWith(profile.Select(Files.Text));
            }
            if (command == "update")
            {
                if (selected.Count == 0)
                {
                    selected.UnionWith(installed.Keys);
                }
                Archive.Require(
                    selected.All(installed.ContainsKey),
                    "Update requires installed components"
                );
                selected.IntersectWith(updates.Select(u => Files.Text(u!["id"])));
                if (selected.Count == 0)
                {
                    return new JsonObject
                    {
                        ["status"] = "ok",
                        ["message"] = "No newer packages; installed versions preserved",
                        ["inventory"] = Files.Json(installed),
                        ["changed"] = false,
                    };
                }
            }
            Archive.Require(selected.Count > 0, "Select at least one package or profile");
            if (command != "remove")
            {
                selected = bundle.Closure(selected);
            }
            foreach (var id in selected)
            {
                if (command == "remove")
                {
                    Archive.Require(
                        installed.ContainsKey(id),
                        "Remove requires installed components"
                    );
                }
                else if (installed.TryGetValue(id, out var v))
                {
                    Archive.Require(
                        Files.Version(Files.Text(bundle.Packages[id]!["version"]))
                            >= Files.Version(v),
                        "Older media cannot downgrade an installed package"
                    );
                }
            }
            foreach (var (id, v) in installed)
            {
                var p = bundle.Packages[id]
                    ?? throw new IOException("Bundle omits installed component");
                if (command != "remove")
                {
                    Archive.Require(
                        Files.Version(Files.Text(p["version"])) >= Files.Version(v),
                        "Bundle is older than the installation"
                    );
                }
                if (command == "remove" && !selected.Contains(id))
                {
                    Archive.Require(
                        !(
                            p["dependencies"]?.AsObject().Any(d => selected.Contains(d.Key))
                            ?? false
                        ),
                        "Cannot remove dependency of installed package " + id
                    );
                }
            }
            var planned = InstalledRecords(state, installed);
            foreach (var id in selected)
            {
                if (command == "remove")
                {
                    planned.Remove(id);
                }
                else
                {
                    var offered = bundle.Packages[id]!;
                    if (
                        planned[id] is JsonNode old
                        && JsonNode.DeepEquals(old["version"], offered["version"])
                    )
                    {
                        Archive.Require(
                            JsonNode.DeepEquals(old, offered),
                            "Publisher changed a package without increasing its version"
                        );
                    }
                    planned[id] = offered.DeepClone();
                }
            }
            // Validate the complete resulting package set, including unselected packages.
            // Download and verify every needed archive before invoking the mutating worker.
            Bundle.ValidatePlan(planned);
            if (online)
            {
                await OnlinePackages.Materialize(
                    bundle,
                    command == "remove" ? [] : selected,
                    onlineCache,
                    source,
                    request.Cached,
                    log
                );
            }
            await Qt(root, state is null, command, selected, bundle.Repositories.Values, log);
            var after = Files.Inventory(root);
            foreach (var id in selected)
            {
                Archive.Require(
                    command == "remove"
                        ? !after.ContainsKey(id)
                        : after.TryGetValue(id, out var v)
                            && v == Files.Text(bundle.Packages[id]!["version"]),
                    "Qt returned an unexpected component version"
                );
            }
            foreach (var (id, v) in installed)
            {
                if (!selected.Contains(id))
                {
                    Archive.Require(
                        after.TryGetValue(id, out var current) && current == v,
                        "Qt changed an unselected component"
                    );
                }
            }
            // Save maintenance assets and new state only after the worker's inventory
            // matches the plan. Preserve the offline registry identity across online updates.
            PersistApplication(root);
            if (snapshot is not null && hash is not null)
            {
                var cached = Path.Combine(root, ".wpilib-manager", hash + ".zip");
                if (!File.Exists(cached) || Files.Hash(cached) != hash)
                {
                    Files.CopyAtomic(snapshot, cached);
                }
            }
            var next = new JsonObject
            {
                ["schema"] = 1,
                ["binding"] = Binding(source),
                ["bundle_sha256"] = hash is null
                    ? state?["bundle_sha256"]?.DeepClone()
                    : JsonValue.Create(hash),
                ["offline_registry"] = online
                    ? (state?["offline_registry"] ?? state?["registry"])?.DeepClone()
                    : bundle.Identity.DeepClone(),
                ["last_source"] = online ? "online" : "offline",
                ["registry"] = bundle.Identity.DeepClone(),
                ["inventory"] = Files.Json(after),
                ["packages"] = planned,
            };
            Storage.Write(statePath, Archive.Canonical(next));
            log(
                "Completed. The installed manager and verified caches are retained for maintenance."
            );
            return new JsonObject
            {
                ["status"] = "ok",
                ["changed"] = true,
                ["inventory"] = Files.Json(after),
                ["registry"] = bundle.Identity.DeepClone(),
                ["manager"] = AppPath(root),
                ["bundle_sha256"] = hash,
            };
        }
    }

    static JsonObject InstalledRecords(
        JsonObject? state,
        SortedDictionary<string, string> inventory
    )
    {
        if (state is null)
        {
            return new JsonObject();
        }
        var saved = state["packages"]!.AsObject();
        Archive.Require(
            saved.Count == inventory.Count
                && inventory.All(p =>
                    saved[p.Key] is JsonNode record
                    && Files.Text(record["id"]) == p.Key
                    && Files.Text(record["version"]) == p.Value
                ),
            "Installed inventory differs from manager state; repair required"
        );
        return saved.DeepClone().AsObject();
    }

    static async Task<JsonObject> Uninstall(
        string root,
        JsonObject? state,
        SortedDictionary<string, string> inventory,
        bool accepted,
        Action<string> log
    )
    {
        Archive.Require(state is not null, "No managed installation");
        Archive.Require(accepted, "Confirm removal with --accept-licenses");
        InstalledRecords(state, inventory);
        using var workspace = new Workspace();
        // Removal uses Qt's installed records and needs no surviving publisher or bundle.
        // An empty local repository also prevents uninstall from initiating a refresh.
        var repo = Path.Combine(workspace.Path, "empty-repository");
        Directory.CreateDirectory(repo);
        File.WriteAllText(
            Path.Combine(repo, "Updates.xml"),
            "<?xml version=\"1.0\"?><Updates></Updates>"
        );
        if (inventory.Count > 0)
        {
            await Qt(root, false, "remove", inventory.Keys, [repo], log);
        }
        Archive.Require(
            Files.Inventory(root).Count == 0,
            "Qt did not remove all installed packages"
        );
        state!["inventory"] = new JsonObject();
        state["packages"] = new JsonObject();
        Storage.Write(Path.Combine(root, ".wpilib-manager.json"), Archive.Canonical(state));
        return new JsonObject
        {
            ["status"] = "ok",
            ["inventory"] = new JsonObject(),
            ["changed"] = inventory.Count > 0,
            ["manager"] = AppPath(root),
            ["message"] =
                "All packages uninstalled. User files, the maintenance application and verified caches are retained.",
        };
    }

    static string QtExecutable(string path) =>
        path.EndsWith(".app", StringComparison.Ordinal)
            ? Directory.GetFiles(Path.Combine(path, "Contents/MacOS")).Single()
            : path;

    public static async Task Qt(
        string root,
        bool fresh,
        string command,
        IEnumerable<string> selected,
        IEnumerable<string> repositories,
        Action<string> log
    )
    {
        var worker = Path.Combine(
            Path.GetDirectoryName(root)!,
            "." + Path.GetFileName(root) + ".qt-worker"
        );
        Archive.Require(
            !Directory.Exists(worker) && !File.Exists(worker),
            "Private Qt workspace already exists; an interrupted operation may need cleanup"
        );
        if (OperatingSystem.IsWindows())
        {
            Directory.CreateDirectory(worker);
        }
        else
        {
            Directory.CreateDirectory(worker, (UnixFileMode)448);
        }
        try
        {
            var engine = EnginePath(root);
            if (fresh)
            {
                using var archive = Resource("engine.zip")!;
                Files.Extract(archive, worker, true);
                engine = Path.Combine(
                    worker,
                    OperatingSystem.IsWindows() ? "engine.exe"
                        : OperatingSystem.IsMacOS() ? "engine.app"
                        : "engine"
                );
            }
            var start = new ProcessStartInfo(QtExecutable(engine))
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            var urls = string.Join(
                ',',
                repositories.Select(p =>
                    new Uri(Path.GetFullPath(p) + Path.DirectorySeparatorChar).AbsoluteUri
                )
            );
            foreach (
                var arg in new[]
                {
                    "--set-temp-repository",
                    urls,
                    "--cache-path",
                    Path.Combine(worker, "qt-cache"),
                    "--type",
                    "package",
                    "--accept-licenses",
                    "--accept-messages",
                    "--confirm-command",
                    "--root",
                    root,
                    command,
                }
            )
            {
                start.ArgumentList.Add(arg);
            }
            foreach (var id in selected.Order(StringComparer.Ordinal))
            {
                start.ArgumentList.Add(id);
            }
            start.Environment["QT_QPA_PLATFORM"] = "offscreen";
            if (OperatingSystem.IsLinux())
            {
                // Keep Qt's private libraries separate from Avalonia and host graphics.
                // This environment belongs only to the worker process.
                var runtime = Path.Combine(worker, "runtime");
                using var archive = Resource("runtime.zip")!;
                Files.Extract(archive, runtime, true);
                start.Environment["LD_LIBRARY_PATH"] = Path.Combine(runtime, "lib");
                start.Environment["FONTCONFIG_FILE"] = Path.Combine(runtime, "share/fonts.conf");
                start.Environment["FONTCONFIG_PATH"] = Path.Combine(runtime, "share");
            }
            log("Applying " + command + ": " + string.Join(", ", selected));
            using var process = Process.Start(start)!;
            async Task Drain(StreamReader reader)
            {
                while (await reader.ReadLineAsync() is string line)
                {
                    log(line);
                }
            }
            await Task.WhenAll(
                Drain(process.StandardOutput),
                Drain(process.StandardError),
                process.WaitForExitAsync()
            );
            Archive.Require(
                process.ExitCode == 0,
                "Qt operation failed; previous bundle cache retained. Inspect the installation before retrying."
            );
            var shortData = Path.Combine(root, ".dat");
            if (File.Exists(shortData))
            {
                Files.CopyAtomic(shortData, Path.Combine(root, ".wpilib-engine.dat"));
                File.Delete(shortData);
            }
        }
        finally
        {
            try
            {
                Directory.Delete(worker, true);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    static void PersistApplication(string root)
    {
        var destination = AppPath(root);
        var current = PublicApplication;
        if (!File.Exists(destination) || Files.Hash(destination) != Files.Hash(current))
        {
            Files.CopyAtomic(current, destination);
        }
        if (OperatingSystem.IsMacOS())
        {
            var plist = Path.Combine(
                Path.GetDirectoryName(Path.GetDirectoryName(current)!)!,
                "Info.plist"
            );
            Files.CopyAtomic(plist, Path.Combine(root, "WPILib Manager.app/Contents/Info.plist"));
        }
    }
}
