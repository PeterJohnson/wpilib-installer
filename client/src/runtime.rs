//! Linux libraries belong to the headless Qt worker only. The manager uses the
//! desktop's graphics stack and never changes its own library search environment.
use crate::trust::Error;
use std::process::Command;
#[cfg(target_os = "linux")]
static RUNTIME: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/runtime.zip"));

pub struct WorkerRuntime {
    #[cfg(target_os = "linux")]
    _directory: tempfile::TempDir,
}

impl WorkerRuntime {
    /// Keep this guard alive until the worker has exited.
    pub fn prepare(command: &mut Command) -> Result<Self, Error> {
        #[cfg(target_os = "linux")]
        {
            use crate::bundle;
            use std::io::Cursor;
            let directory = tempfile::Builder::new()
                .prefix("wpilib-qt-runtime-")
                .tempdir()?;
            bundle::extract(Cursor::new(RUNTIME), directory.path(), true)?;
            let root = directory.path();
            command
                .env("LD_LIBRARY_PATH", root.join("lib"))
                .env("FONTCONFIG_FILE", root.join("share/fonts.conf"))
                .env("FONTCONFIG_PATH", root.join("share"));
            Ok(Self {
                _directory: directory,
            })
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = command;
            Ok(Self {})
        }
    }
}

/// Some Wayland-first desktops omit the X11 keyboard bridge used by winit.
/// Load only the matched keyboard helper libraries by absolute path, without changing
/// the GUI's search path or loading Qt's private libstdc++, X11 or font stack.
pub struct GuiRuntime {
    #[cfg(target_os = "linux")]
    _libraries: Vec<libloading::Library>,
    #[cfg(target_os = "linux")]
    _directory: Option<tempfile::TempDir>,
}

impl GuiRuntime {
    pub fn prepare() -> Result<Self, Error> {
        #[cfg(target_os = "linux")]
        {
            use libloading::Library;
            let mut runtime = Self {
                _libraries: Vec::new(),
                _directory: None,
            };
            if std::env::var_os("DISPLAY").is_none()
                || std::env::var_os("WAYLAND_DISPLAY").is_some()
            {
                return Ok(runtime);
            }
            // SAFETY: These are conventional system libraries. We call no raw
            // symbols; winit owns that API. Handles outlive the complete GUI.
            if let Ok(library) = unsafe { Library::new("libxkbcommon-x11.so.0") } {
                runtime._libraries.push(library);
                return Ok(runtime);
            }
            let directory = tempfile::Builder::new()
                .prefix("wpilib-x11-compat-")
                .tempdir()?;
            let mut archive = zip::ZipArchive::new(std::io::Cursor::new(RUNTIME))?;
            // xkbcommon-x11 uses internal core data structures: keep the core
            // and bridge from the same build, never pair an old bridge with a
            // newer OS core. The GUI has not loaded XKB yet at this point.
            for name in [
                "libxcb-xkb.so.1",
                "libxkbcommon.so.0",
                "libxkbcommon-x11.so.0",
            ] {
                let path = directory.path().join(name);
                let mut output = std::fs::File::create(&path)?;
                std::io::copy(&mut archive.by_name(&format!("lib/{name}"))?, &mut output)?;
                drop(output);
                // SAFETY: Fixed allowlisted files from the compiled archive,
                // never an external bundle. Dependency SONAMEs are reused;
                // handles remain alive until after the GUI exits.
                runtime._libraries.push(unsafe { Library::new(&path) }?);
            }
            runtime._directory = Some(directory);
            Ok(runtime)
        }
        #[cfg(not(target_os = "linux"))]
        {
            Ok(Self {})
        }
    }
}
