//! A private embedded Linux runtime keeps the one-executable UX without FUSE.
//! The parent owns the extraction directory until its child and Qt worker exit.
use crate::trust::Error;
#[cfg(target_os = "linux")]
static RUNTIME: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/runtime.zip"));

pub fn run() -> Result<Option<i32>, Error> {
    #[cfg(target_os = "linux")]
    {
        use crate::bundle;
        use std::{env, io::Cursor, process::Command};
        let marker = bundle::sha(RUNTIME);
        if env::var("WPILIB_PRIVATE_RUNTIME").ok().as_deref() == Some(&marker) {
            return Ok(None);
        }
        let dir = tempfile::Builder::new()
            .prefix("wpilib-runtime-")
            .tempdir()?;
        bundle::extract(Cursor::new(RUNTIME), dir.path(), true)?;
        let root = dir.path();
        let status = Command::new(env::current_exe()?)
            .args(env::args_os().skip(1))
            .env("WPILIB_PRIVATE_RUNTIME", marker)
            .env("LD_LIBRARY_PATH", root.join("lib"))
            .env("LIBGL_DRIVERS_PATH", root.join("dri"))
            .env("LIBGL_ALWAYS_SOFTWARE", "1")
            .env("GALLIUM_DRIVER", "llvmpipe")
            .env("__GLX_VENDOR_LIBRARY_NAME", "mesa")
            .env(
                "__EGL_VENDOR_LIBRARY_FILENAMES",
                root.join("share/egl.json"),
            )
            .env("MESA_SHADER_CACHE_DISABLE", "true")
            .env("FONTCONFIG_FILE", root.join("share/fonts.conf"))
            .env("FONTCONFIG_PATH", root.join("share"))
            .env("XKB_CONFIG_ROOT", root.join("share/X11/xkb"))
            .env("XLOCALEDIR", root.join("share/X11/locale"))
            .status()?;
        return Ok(Some(status.code().unwrap_or(1)));
    }
    #[cfg(not(target_os = "linux"))]
    Ok(None)
}
