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
