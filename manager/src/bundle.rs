use crate::trust::{self, Error};
use aws_lc_rs::digest;
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};
use tempfile::TempDir;

pub fn sha(bytes: &[u8]) -> String {
    digest::digest(&digest::SHA256, bytes)
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn file_hash(path: &Path) -> Result<String, Error> {
    let mut f = fs::File::open(path)?;
    let mut ctx = digest::Context::new(&digest::SHA256);
    let mut buf = [0; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        ctx.update(&buf[..n]);
    }
    Ok(ctx
        .finish()
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}
pub fn strval(v: &Value) -> Result<&str, Error> {
    v.as_str().ok_or_else(|| "Expected string".into())
}
pub fn safe(name: &str) -> Result<(), Error> {
    if name.is_empty()
        || name.chars().any(|c| c < ' ' || "\\:%?#".contains(c))
        || name.split('/').any(|p| {
            p.is_empty()
                || p == "."
                || p == ".."
                || p.ends_with('.')
                || p.ends_with(' ')
                || reserved(p)
        })
    {
        return Err(format!("Unsafe path: {name}").into());
    }
    Ok(())
}
fn reserved(p: &str) -> bool {
    let p = p.split('.').next().unwrap_or("").to_ascii_uppercase();
    matches!(p.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (p.len() == 4
            && (p.starts_with("COM") || p.starts_with("LPT"))
            && matches!(p.as_bytes()[3], b'1'..=b'9'))
}
pub fn version(v: &str) -> Result<Vec<u64>, Error> {
    let mut p = v
        .split('.')
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()?;
    if p.is_empty() || p.len() > 4 {
        return Err("Invalid version".into());
    }
    p.resize(4, 0);
    Ok(p)
}
fn child<'a, 'b>(node: roxmltree::Node<'a, 'b>, name: &str) -> &'a str {
    node.children()
        .find(|c| c.has_tag_name(name))
        .and_then(|c| c.text())
        .unwrap_or("")
}
pub fn inventory(root: &Path) -> Result<BTreeMap<String, String>, Error> {
    let p = root.join("components.xml");
    if !p.exists() {
        return Ok(BTreeMap::new());
    }
    let s = fs::read_to_string(p)?;
    let doc = roxmltree::Document::parse(&s)?;
    let mut result = BTreeMap::new();
    for node in doc
        .root_element()
        .children()
        .filter(|n| n.has_tag_name("Package"))
    {
        let id = child(node, "Name");
        let v = child(node, "Version");
        version(v)?;
        if result.insert(id.into(), v.into()).is_some() {
            return Err("Duplicate installed package".into());
        }
    }
    Ok(result)
}
// Extract into a newly-created private directory, never into the installation.
pub fn extract(
    reader: impl Read + std::io::Seek,
    dest: &Path,
    executable: bool,
) -> Result<(), Error> {
    let mut zip = zip::ZipArchive::new(reader)?;
    if zip.len() > 200_000 {
        return Err("Too many archive members".into());
    }
    let mut seen = BTreeSet::new();
    let mut total = 0u64;
    for i in 0..zip.len() {
        let mut f = zip.by_index(i)?;
        let name = f.name()?.trim_end_matches('/').to_owned();
        safe(&name)?;
        // Outer bundles and engine archives contain regular files/directories only.
        let mode = f.unix_mode().unwrap_or(0);
        if mode & 0o170000 != 0 && mode & 0o170000 != 0o100000 && mode & 0o170000 != 0o040000 {
            return Err("Archive special file rejected".into());
        }
        if !seen.insert(name.to_lowercase()) {
            return Err("Duplicate archive member".into());
        }
        total = total.checked_add(f.size()).ok_or("Archive size overflow")?;
        if total > 100 * 1024 * 1024 * 1024 {
            return Err("Archive exceeds 100 GiB".into());
        }
        let path = dest.join(name);
        if f.is_dir() {
            fs::create_dir_all(path)?;
            continue;
        }
        fs::create_dir_all(path.parent().ok_or("No parent")?)?;
        let mut out = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        let n = std::io::copy(&mut f, &mut out)?;
        out.flush()?;
        if n != f.size() {
            return Err("Truncated ZIP member".into());
        }
        #[cfg(unix)]
        if executable {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                &path,
                fs::Permissions::from_mode(if mode & 0o111 != 0 { 0o700 } else { 0o600 }),
            )?;
        }
    }
    let _ = executable;
    Ok(())
}
pub struct Bundle {
    pub _temp: TempDir,
    pub registry: Value,
    pub identity: Value,
    pub packages: BTreeMap<String, Value>,
    pub repos: Vec<PathBuf>,
}
impl Bundle {
    pub fn open(path: &Path, source: &Value, log: &dyn Fn(String)) -> Result<Self, Error> {
        log("Extracting offline bundle into a private verification workspace…".into());
        let temp = tempfile::tempdir()?;
        extract(fs::File::open(path)?, temp.path(), false)?;
        let dirs = fs::read_dir(temp.path())?.collect::<Result<Vec<_>, _>>()?;
        if dirs.len() != 1 || !dirs[0].file_type()?.is_dir() {
            return Err("Expected one bundle directory".into());
        }
        let root = dirs[0].path();
        let identity = trust::authorize(source, &root)?;
        let record = trust::read(root.join("registry.json"))?;
        let registry = record["signed"]["registry"].clone();
        let manifest = trust::read(root.join("bundle-manifest.json"))?;
        let b = &manifest["signed"];
        for field in ["target", "season", "channel"] {
            if b[field] != registry[field] {
                return Err("Bundle scope mismatch".into());
            }
        }
        let publishers = registry["repositories"]
            .as_object()
            .ok_or("No publishers")?;
        if publishers.len()
            != b["repositories"]
                .as_object()
                .ok_or("No bundle publishers")?
                .len()
        {
            return Err("Publisher set mismatch".into());
        }
        let mut packages = BTreeMap::new();
        let mut repos = vec![];
        let mut owned = BTreeSet::new();
        let mut allow = BTreeSet::new();
        for (publisher, policy) in publishers {
            safe(publisher)?;
            if publisher.contains('/') {
                return Err("Invalid publisher".into());
            }
            let allowed = policy["components"]
                .as_array()
                .ok_or("Missing component allowlist")?;
            for id in allowed {
                if !allow.insert(strval(id)?.to_owned()) {
                    return Err("Ambiguous component owner".into());
                }
            }
            let repo = root.join("repos").join(publisher);
            let envelope = trust::read(repo.join("release.json"))?;
            let m = trust::signed(&envelope, &policy["public_key"])?;
            if m["schema"] != 1
                || m["publisher"] != *publisher
                || m["revision"] != b["repositories"][publisher]["revision"]
                || sha(&trust::canonical(&envelope)?)
                    != strval(&b["repositories"][publisher]["sha256"])?
            {
                return Err("Release identity mismatch".into());
            }
            if m["revision"].as_u64().unwrap_or(0)
                < policy["minimum_revision"].as_u64().unwrap_or(1)
            {
                return Err("Release below revision floor".into());
            }
            for field in ["target", "season", "channel"] {
                if m[field] != registry[field] {
                    return Err("Release scope mismatch".into());
                }
            }
            let files = m["files"].as_object().ok_or("No files")?;
            log(format!("Verifying every file from {publisher}…"));
            for (name, record) in files {
                safe(name)?;
                let file = repo.join(name);
                if fs::symlink_metadata(&file)?.file_type().is_symlink()
                    || fs::metadata(&file)?.len()
                        != record["size"].as_u64().ok_or("Invalid file size")?
                    || file_hash(&file)? != strval(&record["sha256"])?
                {
                    return Err(format!("Corrupt repository file: {name}").into());
                }
            }
            let mut expected = BTreeMap::new();
            for p in m["packages"].as_array().ok_or("No packages")? {
                let id = strval(&p["id"])?;
                if !allowed.contains(&p["id"])
                    || id.is_empty()
                    || !id
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.')
                    || packages.insert(id.into(), p.clone()).is_some()
                {
                    return Err("Unauthorized/duplicate component".into());
                }
                version(strval(&p["version"])?)?;
                expected.insert(id, p);
                let records = p["file_records"]
                    .as_object()
                    .ok_or("Payload records required")?;
                let names = p["owned_files"].as_array().ok_or("Ownership required")?;
                if records.len() != names.len() {
                    return Err("Ownership records differ".into());
                }
                for name in names {
                    let name = strval(name)?;
                    safe(name)?;
                    // Reserve the manager state and Qt inventory from package ownership.
                    let top = name.split('/').next().unwrap().to_ascii_lowercase();
                    if top.starts_with(".wpilib")
                        || matches!(
                            top.as_str(),
                            "wpilib-manager"
                                | "wpilibmanager.exe"
                                | "wpilib manager.app"
                                | "components.xml"
                        )
                    {
                        return Err("Package owns manager state".into());
                    }
                    if !records.contains_key(name) || !owned.insert(name.to_lowercase()) {
                        return Err("Conflicting package ownership".into());
                    }
                }
            }
            let xml = fs::read_to_string(repo.join("Updates.xml"))?;
            let doc = roxmltree::Document::parse(&xml)?;
            let mut seen = BTreeSet::new();
            if doc.descendants().any(|n| {
                n.has_tag_name("RepositoryUpdate") || n.has_tag_name("RepositoryCategories")
            }) {
                return Err("Repository redirects forbidden".into());
            }
            for node in doc
                .root_element()
                .children()
                .filter(|n| n.has_tag_name("PackageUpdate"))
            {
                let id = child(node, "Name");
                let p = expected.get(id).ok_or("Unknown Qt package")?;
                if !seen.insert(id) || child(node, "Version") != strval(&p["version"])? {
                    return Err("Qt manifest mismatch".into());
                }
                let deps = p["dependencies"]
                    .as_object()
                    .map(|d| {
                        d.iter()
                            .map(|(k, v)| Ok(format!("{k}->={}", strval(v)?)))
                            .collect::<Result<BTreeSet<_>, Error>>()
                    })
                    .transpose()?
                    .unwrap_or_default();
                if deps
                    != child(node, "Dependencies")
                        .split(',')
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .collect()
                {
                    return Err("Qt dependency mismatch".into());
                }
                for archive in child(node, "DownloadableArchives")
                    .split(',')
                    .filter(|s| !s.is_empty())
                {
                    if !files.contains_key(&format!("{id}/{}{archive}", strval(&p["version"])?)) {
                        return Err("Unsigned Qt archive".into());
                    }
                }
            }
            if seen.len() != expected.len() {
                return Err("Incomplete Qt index".into());
            }
            repos.push(repo);
        }
        for name in &owned {
            let mut at = name.as_str();
            while let Some((parent, _)) = at.rsplit_once('/') {
                if owned.contains(parent) {
                    return Err("File/directory ownership conflict".into());
                }
                at = parent;
            }
        }
        for p in packages.values() {
            if let Some(deps) = p["dependencies"].as_object() {
                for (id, min) in deps {
                    let dep = packages.get(id).ok_or("Missing dependency")?;
                    if version(strval(&dep["version"])?)? < version(strval(min)?)? {
                        return Err("Incompatible bundle".into());
                    }
                }
            }
        }
        for ids in registry["profiles"]
            .as_object()
            .ok_or("No profiles")?
            .values()
        {
            for id in ids.as_array().ok_or("Invalid profile")? {
                if !packages.contains_key(strval(id)?) {
                    return Err("Profile requires absent package".into());
                }
            }
        }
        log(
            "Bundle signatures, registry identity, package hashes and dependencies verified."
                .into(),
        );
        Ok(Self {
            _temp: temp,
            registry,
            identity,
            packages,
            repos,
        })
    }
    pub fn closure(&self, selected: &[String]) -> Result<BTreeSet<String>, Error> {
        let mut result = BTreeSet::new();
        let mut todo = selected.to_vec();
        while let Some(id) = todo.pop() {
            if result.contains(&id) {
                continue;
            }
            let p = self
                .packages
                .get(&id)
                .ok_or("Unknown requested component")?;
            result.insert(id);
            if let Some(d) = p["dependencies"].as_object() {
                todo.extend(d.keys().cloned())
            }
        }
        Ok(result)
    }
}

/// Check the final installed set, including unchanged versions from local state.
pub fn validate_plan(packages: &BTreeMap<String, Value>) -> Result<(), Error> {
    let mut owned = BTreeSet::new();
    for p in packages.values() {
        if let Some(deps) = p["dependencies"].as_object() {
            for (id, minimum) in deps {
                let dependency = packages
                    .get(id)
                    .ok_or("Plan would remove a required dependency")?;
                if version(strval(&dependency["version"])?)? < version(strval(minimum)?)? {
                    return Err("Plan does not satisfy installed dependencies".into());
                }
            }
        }
        for name in p["owned_files"]
            .as_array()
            .ok_or("Missing installed ownership records")?
        {
            let name = strval(name)?;
            safe(name)?;
            if !owned.insert(name.to_lowercase()) {
                return Err("Plan would overwrite another installed package's files".into());
            }
        }
    }
    for name in &owned {
        let mut at = name.as_str();
        while let Some((parent, _)) = at.rsplit_once('/') {
            if owned.contains(parent) {
                return Err("Plan has overlapping file and directory ownership".into());
            }
            at = parent;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn partial_upgrades_preserve_old_version_dependencies_and_ownership() {
        let mut plan = BTreeMap::from([
            (
                "a".into(),
                serde_json::json!({"version":"1.0", "dependencies":{"runtime":"2.0"}, "owned_files":["tools/a"]}),
            ),
            (
                "runtime".into(),
                serde_json::json!({"version":"1.0", "owned_files":["tools/runtime"]}),
            ),
        ]);
        assert!(validate_plan(&plan).is_err());
        plan.get_mut("runtime").unwrap()["version"] = serde_json::json!("2.0");
        assert!(validate_plan(&plan).is_ok());
        plan.get_mut("runtime").unwrap()["owned_files"] = serde_json::json!(["tools/A"]);
        assert!(validate_plan(&plan).is_err());
        plan.remove("runtime");
        assert!(validate_plan(&plan).is_err());
    }
    #[test]
    fn portable_paths_reject_windows_aliases_and_traversal() {
        for name in [
            "../escape",
            "a/../../x",
            "/absolute",
            "C:/temp",
            "a\\b",
            "x/NUL.txt",
            "COM1",
            "a/./b",
            "a//b",
            "trailing.",
            "file ",
            "a%2fb",
        ] {
            assert!(safe(name).is_err(), "{name}");
        }
        assert!(safe("org.wpilib.elastic/1.0.0data.7z").is_ok());
    }
    #[test]
    fn versions_compare_numerically_without_truncation() {
        assert!(version("2.10").unwrap() > version("2.9.99").unwrap());
        assert_eq!(version("1.0").unwrap(), version("1.0.0.0").unwrap());
        for v in ["1.a", "1.2.3.4.5", "", "18446744073709551616"] {
            assert!(version(v).is_err());
        }
    }
    #[test]
    fn zip_extraction_rejects_traversal_before_writing() {
        let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        writer
            .start_file("../escape", zip::write::SimpleFileOptions::default())
            .unwrap();
        writer.write_all(b"bad").unwrap();
        let bytes = writer.finish().unwrap();
        let tmp = tempfile::tempdir().unwrap();
        assert!(extract(bytes, tmp.path(), false).is_err());
        assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
    }
    #[test]
    fn zip_extraction_rejects_duplicate_case_aliases() {
        let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        for path in ["bundle/file", "bundle/FILE"] {
            writer
                .start_file(path, zip::write::SimpleFileOptions::default())
                .unwrap();
            writer.write_all(b"x").unwrap();
        }
        let bytes = writer.finish().unwrap();
        let tmp = tempfile::tempdir().unwrap();
        assert!(extract(bytes, tmp.path(), false).is_err());
    }
}
