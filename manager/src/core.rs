use crate::{
    bundle::{self, Bundle},
    trust::{self, Error},
};
use fs2::FileExt;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub static ENGINE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/engine.zip"));
pub fn source() -> Result<Value, Error> {
    Ok(serde_json::from_slice(include_bytes!(concat!(
        env!("OUT_DIR"),
        "/bootstrap.json"
    )))?)
}
pub fn target() -> String {
    format!(
        "{}-{}",
        if cfg!(target_os = "windows") {
            "windows"
        } else if cfg!(target_os = "macos") {
            "macos"
        } else {
            "linux"
        },
        if cfg!(target_arch = "aarch64") {
            "arm64"
        } else {
            "x64"
        }
    )
}
pub fn app_path(root: &Path) -> PathBuf {
    root.join(if cfg!(windows) {
        "WPILibManager.exe"
    } else if cfg!(target_os = "macos") {
        "WPILib Manager.app/Contents/MacOS/wpilib-manager"
    } else {
        "wpilib-manager"
    })
}
pub fn default_root() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        let parent = if cfg!(target_os = "macos") {
            exe.ancestors().nth(4)
        } else {
            exe.parent()
        };
        if let Some(p) = parent {
            if p.join(".wpilib-manager.json").is_file() {
                return p.to_path_buf();
            }
        }
    }
    PathBuf::from(
        std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap_or_default(),
    )
    .join("wpilib-rust-prototype")
}
pub fn qt_executable(path: &Path) -> Result<PathBuf, Error> {
    if path.extension().is_some_and(|p| p == "app") {
        let files = fs::read_dir(path.join("Contents/MacOS"))?.collect::<Result<Vec<_>, _>>()?;
        if files.len() != 1 {
            return Err("Ambiguous Qt application executable".into());
        }
        Ok(files[0].path())
    } else {
        Ok(path.to_path_buf())
    }
}
pub fn engine_path(root: &Path) -> PathBuf {
    root.join(if cfg!(windows) {
        ".wpilib-engine.exe"
    } else if cfg!(target_os = "macos") {
        ".wpilib-engine.app"
    } else {
        ".wpilib-engine"
    })
}
fn atomic(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    let mut f = tempfile::NamedTempFile::new_in(path.parent().ok_or("Missing parent")?)?;
    f.write_all(bytes)?;
    f.as_file().sync_all()?;
    f.persist(path)?;
    Ok(())
}
#[derive(Clone)]
pub struct Request {
    pub command: String,
    pub root: PathBuf,
    pub bundle: Option<PathBuf>,
    pub packages: Vec<String>,
    pub profile: Option<String>,
    pub accept_licenses: bool,
}
fn binding(source: &Value) -> Value {
    json!({"registry_id":source["registry_id"],"season":source["season"],"channel":source["channel"],"target":source["target"],"archive_key":source["public_key"]})
}
pub fn execute(req: &Request, log: &(dyn Fn(String) + Sync)) -> Result<Value, Error> {
    let source = source()?;
    if source["target"] != target() {
        return Err("Application bootstrap target differs from host".into());
    }
    let root = std::path::absolute(&req.root)?;
    let parent = root.parent().ok_or("Invalid install root")?;
    fs::create_dir_all(parent)?;
    // A sibling lock survives even if Qt removes its target directory.
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(format!(
            ".{}.manager-lock",
            root.file_name().ok_or("Invalid root")?.to_string_lossy()
        )))?;
    lock.try_lock_exclusive()
        .map_err(|_| "Another manager operation is active for this installation")?;
    let state_path = root.join(".wpilib-manager.json");
    let state = if state_path.exists() {
        Some(trust::read(&state_path)?)
    } else {
        None
    };
    if let Some(s) = &state {
        if s["binding"] != binding(&source) {
            return Err("Installation belongs to a different bootstrap trust/scope".into());
        }
    }
    let installed = bundle::inventory(&root)?;
    if !installed.is_empty() && state.is_none() {
        return Err("Unmanaged Qt installation; migration is not implemented".into());
    }
    if req.command == "list" {
        return Ok(json!({"status":"ok","inventory":installed,"installation":root}));
    }
    let bundle_path = if let Some(p) = &req.bundle {
        p.clone()
    } else {
        let s = state.as_ref().ok_or("Choose an offline bundle ZIP")?;
        let hash = bundle::strval(&s["bundle_sha256"])?;
        if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("Invalid cached bundle identity".into());
        }
        root.join(".wpilib-manager").join(format!("{hash}.zip"))
    };
    // Snapshot source media before verification and publication into the cache.
    let snapshot = tempfile::NamedTempFile::new()?;
    fs::copy(&bundle_path, snapshot.path())?;
    let hash = bundle::file_hash(snapshot.path())?;
    if req.bundle.is_none() && state.as_ref().is_some_and(|s| s["bundle_sha256"] != hash) {
        return Err("Cached bundle digest mismatch".into());
    }
    let bundle = Bundle::open(snapshot.path(), &source, log)?;
    let updates = installed
        .iter()
        .filter_map(|(id, current)| bundle.packages.get(id).map(|p| (id, current, p)))
        .filter(|(_, v, p)| {
            bundle::version(bundle::strval(&p["version"]).unwrap()).unwrap()
                > bundle::version(v).unwrap()
        })
        .map(|(id, current, p)| json!({"id":id,"installed":current,"available":p["version"]}))
        .collect::<Vec<_>>();
    if matches!(req.command.as_str(), "verify" | "check-updates") {
        return Ok(
            json!({"protocol_version":1,"status":"ok","installation":root,"inventory":installed,"updates":updates,"packages":bundle.packages,"profiles":bundle.registry["profiles"],"registry":bundle.identity,"bundle_sha256":hash}),
        );
    }
    if !matches!(req.command.as_str(), "install" | "update" | "remove") {
        return Err("Unsupported command".into());
    }
    if !req.accept_licenses {
        return Err("Review package licenses and explicitly accept before applying changes".into());
    }
    if state.is_none() && req.command != "install" {
        return Err("Install packages before maintenance".into());
    }
    if state.is_none() && root.exists() && fs::read_dir(&root)?.next().is_some() {
        return Err("Fresh installation needs an empty directory".into());
    }
    let mut selected = req.packages.clone();
    if let Some(profile) = &req.profile {
        if req.command != "install" {
            return Err("Profiles are only valid for install".into());
        }
        selected.extend(
            bundle.registry["profiles"][profile]
                .as_array()
                .ok_or("Unknown profile")?
                .iter()
                .map(|id| bundle::strval(id).map(str::to_owned))
                .collect::<Result<Vec<_>, _>>()?,
        );
    }
    if req.command == "update" {
        if selected.is_empty() {
            selected.extend(installed.keys().cloned())
        }
        if selected.iter().any(|id| !installed.contains_key(id)) {
            return Err("Update requires installed components".into());
        }
        selected.retain(|id| updates.iter().any(|u| u["id"] == *id));
        if selected.is_empty() {
            return Ok(
                json!({"status":"ok","message":"No newer packages; installed versions preserved","inventory":installed,"changed":false}),
            );
        }
    }
    if selected.is_empty() {
        return Err("Select at least one package or profile".into());
    }
    let selected: BTreeSet<String> = if req.command == "remove" {
        selected.into_iter().collect()
    } else {
        bundle.closure(&selected)?
    };
    for id in &selected {
        if req.command == "remove" {
            if !installed.contains_key(id) {
                return Err("Remove requires installed components".into());
            }
        } else if let Some(v) = installed.get(id) {
            if bundle::version(bundle::strval(&bundle.packages[id]["version"])?)?
                < bundle::version(v)?
            {
                return Err("Older media cannot downgrade an installed package".into());
            }
        }
    }
    // Keep a complete compatible description of installed packages for maintenance.
    for (id, v) in &installed {
        let p = bundle
            .packages
            .get(id)
            .ok_or("Bundle omits an installed component; use a complete compatible bundle")?;
        if req.command != "remove"
            && bundle::version(bundle::strval(&p["version"])?)? < bundle::version(v)?
        {
            return Err("Bundle is older than the installation".into());
        }
        if req.command == "remove" && !selected.contains(id) {
            if let Some(deps) = p["dependencies"].as_object() {
                if deps.keys().any(|d| selected.contains(d)) {
                    return Err(
                        format!("Cannot remove a dependency of installed package {id}").into(),
                    );
                }
            }
        }
    }
    let private = Worker::new(&root)?;
    let engine = if state.is_some() {
        qt_executable(&engine_path(&root))?
    } else {
        bundle::extract(std::io::Cursor::new(ENGINE), &private.0, true)?;
        qt_executable(&private.0.join(if cfg!(windows) {
            "engine.exe"
        } else if cfg!(target_os = "macos") {
            "engine.app"
        } else {
            "engine"
        }))?
    };
    let urls = bundle
        .repos
        .iter()
        .map(|p| {
            url::Url::from_directory_path(p)
                .map(|u| u.to_string())
                .map_err(|_| "Invalid repository directory")
        })
        .collect::<Result<Vec<_>, _>>()?
        .join(",");
    let qt_cache = tempfile::tempdir()?;
    let mut cmd = Command::new(&engine);
    cmd.args(["--set-temp-repository", &urls, "--cache-path"])
        .arg(qt_cache.path())
        .args([
            "--type",
            "package",
            "--accept-licenses",
            "--accept-messages",
            "--confirm-command",
            "--root",
        ])
        .arg(&root)
        .arg(&req.command)
        .args(&selected)
        .env("QT_QPA_PLATFORM", "offscreen")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    log(format!(
        "Applying {}: {}",
        req.command,
        selected.iter().cloned().collect::<Vec<_>>().join(", ")
    ));
    let mut child = cmd.spawn()?;
    let out = child.stdout.take().ok_or("Missing Qt output")?;
    let err = child.stderr.take().ok_or("Missing Qt error output")?;
    std::thread::scope(|scope| {
        scope.spawn(|| {
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                log(line)
            }
        });
        scope.spawn(|| {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                log(line)
            }
        });
    });
    let status = child.wait()?;
    if !status.success() {
        return Err(format!("Qt operation failed ({status}). Previous bundle cache was retained; inspect installation before retrying.").into());
    }
    // IFW 4.11 uses QFileInfo::baseName while writing the data sidecar,
    // which strips a leading-dot maintenance basename to an empty string.
    // Its reader expects the complete basename. Normalize this private sidecar
    // after every successful operation, including maintenance rewrites.
    let short_data = root.join(".dat");
    if short_data.is_file() {
        atomic(&root.join(".wpilib-engine.dat"), &fs::read(&short_data)?)?;
        fs::remove_file(short_data)?;
    }
    let after = bundle::inventory(&root)?;
    for id in &selected {
        if req.command == "remove" {
            if after.contains_key(id) {
                return Err("Qt did not remove selected component".into());
            }
        } else if after.get(id).map(String::as_str)
            != Some(bundle::strval(&bundle.packages[id]["version"])?)
        {
            return Err("Qt installed an unexpected version".into());
        }
    }
    for (id, v) in &installed {
        if !selected.contains(id) && after.get(id) != Some(v) {
            return Err("Qt changed an unselected component".into());
        }
    }
    persist_app(&root)?;
    let cache = root.join(".wpilib-manager");
    fs::create_dir_all(&cache)?;
    let cached = cache.join(format!("{hash}.zip"));
    if !cached.exists() || bundle::file_hash(&cached)? != hash {
        let mut f = tempfile::NamedTempFile::new_in(&cache)?;
        std::io::copy(&mut fs::File::open(snapshot.path())?, &mut f)?;
        f.as_file().sync_all()?;
        f.persist(&cached)?;
    }
    atomic(
        &state_path,
        &serde_json::to_vec_pretty(
            &json!({"schema":1,"binding":binding(&source),"bundle_sha256":hash,"registry":bundle.identity,"inventory":after}),
        )?,
    )?;
    log(
        "Completed. The installed manager and verified offline media are retained for maintenance."
            .into(),
    );
    Ok(
        json!({"status":"ok","changed":true,"inventory":after,"registry":bundle.identity,"manager":app_path(&root),"bundle_sha256":hash}),
    )
}
fn persist_app(root: &Path) -> Result<(), Error> {
    let current = std::env::current_exe()?;
    let dest = app_path(root);
    fs::create_dir_all(dest.parent().ok_or("No application parent")?)?;
    if !dest.exists() || bundle::file_hash(&dest)? != bundle::file_hash(&current)? {
        let mut f = tempfile::NamedTempFile::new_in(dest.parent().unwrap())?;
        std::io::copy(&mut fs::File::open(current)?, &mut f)?;
        f.as_file()
            .set_permissions(fs::metadata(std::env::current_exe()?)?.permissions())?;
        f.persist(&dest)?;
    }
    #[cfg(target_os = "macos")]
    {
        atomic(
            &root.join("WPILib Manager.app/Contents/Info.plist"),
            include_bytes!("../Info.plist"),
        )?;
    }
    Ok(())
}

// A deterministic, per-installation worker path permits program-scoped offline
// firewall testing on Windows. create_dir refuses existing directories/symlinks;
// the installation lock prevents concurrent manager access.
struct Worker(PathBuf);
impl Worker {
    fn new(root: &Path) -> Result<Self, Error> {
        let path = root.parent().ok_or("Missing root parent")?.join(format!(
            ".{}.qt-worker",
            root.file_name()
                .ok_or("Missing root name")?
                .to_string_lossy()
        ));
        fs::create_dir(&path).map_err(|e|format!("Cannot create private Qt workspace {}: {e}. A previous interrupted run may need cleanup.",path.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        }
        Ok(Self(path))
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
