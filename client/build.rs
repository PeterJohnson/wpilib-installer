use std::{env, fs, path::PathBuf};
fn main() {
    println!("cargo:rerun-if-env-changed=WPILIB_ASSET_DIR");
    let assets = PathBuf::from(
        env::var("WPILIB_ASSET_DIR")
            .expect("Run tools/build_client.py to supply pinned bootstrap and private engine"),
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let mut names = vec!["bootstrap.json", "engine.zip"];
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux") {
        names.push("runtime.zip");
    }
    for name in names {
        let source = assets.join(name);
        println!("cargo:rerun-if-changed={}", source.display());
        fs::copy(source, out.join(name)).unwrap();
    }
}
