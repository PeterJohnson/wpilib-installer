//! TUF-authenticated online catalogs. Transport caches are untrusted: every use
//! replays the original root chain. Historical verification establishes rollback
//! floors only; all usable live/cache views enforce the actual current clock.
use crate::{
    bundle::{self, Bundle},
    trust::{self, Error},
};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tough::{
    ExpirationEnforcement, IntoVec, Limits, RepositoryLoader, TargetName, Transport,
    TransportError, TransportErrorKind, TransportStream,
};
use url::Url;
const MAX_META: u64 = 16 * 1024 * 1024;
const MAX_CATALOG: u64 = 8 * 1024 * 1024;
const ROOT_BATCH: usize = 256;
const BATCHES: usize = 4;
pub fn atomic(path: &Path, data: &[u8]) -> Result<(), Error> {
    fs::create_dir_all(path.parent().ok_or("Missing parent")?)?;
    let mut f = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    f.write_all(data)?;
    f.as_file().sync_all()?;
    f.persist(path)?;
    Ok(())
}
fn now() -> Result<u64, Error> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}
fn hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn url(value: &str, dev: bool) -> Result<Url, Error> {
    let u = Url::parse(value)?;
    if !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
        || u.host_str().is_none()
    {
        return Err("Invalid repository URL".into());
    }
    if u.scheme() != "https"
        && !(dev && u.scheme() == "http" && matches!(u.host_str(), Some("127.0.0.1" | "[::1]")))
    {
        return Err(
            "Repositories require HTTPS (only compiled test bootstraps permit loopback HTTP)"
                .into(),
        );
    }
    Ok(u)
}
fn client(dev: bool) -> Result<reqwest::Client, Error> {
    Ok(reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(120))
        .redirect(reqwest::redirect::Policy::custom(move |attempt| {
            if attempt.previous().len() > 5 || url(attempt.url().as_str(), dev).is_err() {
                attempt.error("Forbidden redirect")
            } else {
                attempt.follow()
            }
        }))
        .build()?)
}
#[derive(Debug, Clone, Copy, PartialEq)]
enum Failure {
    Missing,
    Unavailable,
    Invalid,
    Pending,
}
#[derive(Debug)]
struct DownloadError(Failure, String);
impl std::fmt::Display for DownloadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.1)
    }
}
impl std::error::Error for DownloadError {}
async fn fetch(http: &reqwest::Client, u: Url, limit: u64) -> Result<Vec<u8>, DownloadError> {
    let mut response = http.get(u.clone()).send().await.map_err(|e| {
        DownloadError(
            if e.is_connect() || e.is_timeout() {
                Failure::Unavailable
            } else {
                Failure::Invalid
            },
            format!("HTTP transport: {e}"),
        )
    })?;
    let status = response.status();
    if !status.is_success() {
        return Err(DownloadError(
            if status.as_u16() == 404 {
                Failure::Missing
            } else if status.is_server_error() {
                Failure::Unavailable
            } else {
                Failure::Invalid
            },
            format!("HTTP {status} at {u}"),
        ));
    }
    if response.content_length().is_some_and(|n| n > limit) {
        return Err(DownloadError(
            Failure::Invalid,
            "Oversized HTTP response".into(),
        ));
    }
    let mut data = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|e| DownloadError(Failure::Unavailable, e.to_string()))?
    {
        if data.len() as u64 + chunk.len() as u64 > limit {
            return Err(DownloadError(
                Failure::Invalid,
                "Oversized HTTP response".into(),
            ));
        }
        data.extend_from_slice(&chunk);
    }
    Ok(data)
}
#[derive(Debug, Default)]
struct Progress {
    fetched: usize,
    complete: bool,
    failure: Option<Failure>,
}
#[derive(Debug, Clone)]
struct CatalogTransport {
    base: Url,
    roots: PathBuf,
    responses: PathBuf,
    offline: bool,
    stop_root: Option<u64>,
    http: reqwest::Client,
    progress: Arc<Mutex<Progress>>,
}
impl CatalogTransport {
    async fn bytes(&self, u: &Url) -> Result<Vec<u8>, DownloadError> {
        let name = u
            .as_str()
            .strip_prefix(self.base.as_str())
            .ok_or_else(|| DownloadError(Failure::Invalid, "Unexpected TUF URL".into()))?;
        bundle::safe(name).map_err(|e| DownloadError(Failure::Invalid, e.to_string()))?;
        let root_version = name
            .strip_prefix("metadata/")
            .and_then(|n| n.strip_suffix(".root.json"))
            .and_then(|v| v.parse::<u64>().ok());
        let missing = || DownloadError(Failure::Missing, "TUF metadata not found".into());
        if let Some(version) = root_version {
            if self.stop_root.is_some_and(|stop| version > stop) {
                self.progress.lock().unwrap().complete = true;
                return Err(missing());
            }
            let p = self.roots.join(format!("{version}.root.json"));
            if p.exists() {
                return read_limited(&p, MAX_META)
                    .map_err(|e| DownloadError(Failure::Invalid, e.to_string()));
            }
            if self.offline {
                return Err(DownloadError(
                    Failure::Invalid,
                    "Missing cached root history".into(),
                ));
            }
            if self.progress.lock().unwrap().fetched >= ROOT_BATCH {
                return Err(DownloadError(
                    Failure::Pending,
                    "root-update-pending: retry to continue verified catch-up".into(),
                ));
            }
        } else if !self.progress.lock().unwrap().complete {
            return Err(DownloadError(
                Failure::Pending,
                "Root catch-up is incomplete".into(),
            ));
        }
        let p = self.responses.join(name);
        if self.offline {
            return read_limited(&p, MAX_META).map_err(|e| {
                DownloadError(Failure::Invalid, format!("Incomplete verified cache: {e}"))
            });
        }
        match fetch(&self.http, u.clone(), MAX_META).await {
            Ok(data) => {
                let dest = if let Some(v) = root_version {
                    self.progress.lock().unwrap().fetched += 1;
                    self.roots.join(format!("{v}.root.json"))
                } else {
                    p
                };
                atomic(&dest, &data).map_err(|e| DownloadError(Failure::Invalid, e.to_string()))?;
                Ok(data)
            }
            Err(e) => {
                if root_version.is_some() && e.0 == Failure::Missing {
                    self.progress.lock().unwrap().complete = true;
                }
                Err(e)
            }
        }
    }
}
#[async_trait]
impl Transport for CatalogTransport {
    async fn fetch(&self, u: Url) -> Result<TransportStream, TransportError> {
        match self.bytes(&u).await {
            Ok(bytes) => Ok(Box::pin(futures::stream::once(
                async move { Ok(bytes.into()) },
            ))),
            Err(e) => {
                if e.0 != Failure::Missing {
                    self.progress.lock().unwrap().failure = Some(e.0)
                }
                let error = TransportError::new_with_cause(
                    if e.0 == Failure::Missing {
                        TransportErrorKind::FileNotFound
                    } else {
                        TransportErrorKind::Other
                    },
                    u.as_str(),
                    e,
                );
                // tough treats initial fetch errors as end-of-root-chain. Only
                // a real 404 may use that path; other failures travel in-stream.
                if error.kind() == TransportErrorKind::FileNotFound {
                    Err(error)
                } else {
                    Ok(Box::pin(futures::stream::once(async move { Err(error) })))
                }
            }
        }
    }
}
fn read_limited(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    if fs::symlink_metadata(path)?.file_type().is_symlink() || fs::metadata(path)?.len() > max {
        return Err("Invalid/oversized cache file".into());
    }
    Ok(fs::read(path)?)
}
fn describe(error: &dyn std::error::Error) -> String {
    let mut result = error.to_string();
    let mut next = error.source();
    while let Some(e) = next {
        result.push_str(" <- ");
        result.push_str(&e.to_string());
        next = e.source();
    }
    result
}
struct Catalog {
    value: Value,
    previous: Option<Value>,
    status: &'static str,
    root_version: u64,
    accepted: PathBuf,
    head: Value,
}
impl Catalog {
    fn accept(&self) -> Result<(), Error> {
        atomic(&self.accepted, &serde_json::to_vec(&self.head)?)
    }
}
fn proof_path(dir: &Path, head: &Value) -> Result<PathBuf, Error> {
    let generation = bundle::strval(&head["generation"])?;
    let proof = bundle::strval(&head["proof"])?;
    if !hex(generation) || !hex(proof) {
        return Err("Invalid cache proof reference".into());
    }
    let path = dir.join("generations").join(generation).join(proof);
    let mut files = BTreeMap::new();
    collect_files(&path, &path, &mut files)?;
    if bundle::sha(&trust::canonical(&json!(files))?) != proof {
        return Err("Corrupt verified metadata cache".into());
    }
    Ok(path)
}
async fn historical(
    root: &Vec<u8>,
    base: &Url,
    dir: &Path,
    head: &Value,
    http: &reqwest::Client,
    datastore: &Path,
    initial: u64,
) -> Result<Value, Error> {
    let responses = proof_path(dir, head)?;
    let stop = head["root_version"]
        .as_u64()
        .filter(|v| *v >= initial && *v - initial <= 1_000_000)
        .ok_or("Invalid cached root version")?;
    let transport = CatalogTransport {
        base: base.clone(),
        roots: dir.join("roots"),
        responses,
        offline: true,
        stop_root: Some(stop),
        http: http.clone(),
        progress: Arc::default(),
    };
    // Only verifies historical rollback evidence. Neither callers nor the GUI
    // may use this result as a usable current-clock repository view.
    let repo = RepositoryLoader::new(root, base.join("metadata/")?, base.join("targets/")?)
        .transport(transport)
        .datastore(datastore)
        .limits(Limits {
            max_root_updates: stop - initial + 2,
            ..Limits::default()
        })
        .expiration_enforcement(ExpirationEnforcement::Unsafe)
        .load()
        .await?;
    if repo.root().signed.version.get() != stop {
        return Err("Cached root proof mismatch".into());
    }
    let data = repo
        .read_target(&TargetName::new("catalog.json")?)
        .await?
        .ok_or("Missing cached catalog")?
        .into_vec()
        .await?;
    if data.len() as u64 > MAX_CATALOG || bundle::sha(&data) != head["generation"] {
        return Err("Cached catalog proof mismatch".into());
    }
    Ok(serde_json::from_slice(&data)?)
}
// A checkpoint contains only metadata that tough has verified. On every new
// process, re-authenticate it from the compiled root (including both thresholds
// at every transition) before seeding tough's rollback datastore. Starting the
// live loader at that authenticated root avoids replaying historical key changes
// as though they were NEW rotations, which would clear rollback floors.
fn authenticate_checkpoint(pin: &[u8], roots: &Path, checkpoint: &Value) -> Result<Vec<u8>, Error> {
    use tough::schema::{Root, Signed, Snapshot, Timestamp};
    let mut root: Signed<Root> = serde_json::from_slice(pin)?;
    root.signed.verify_role(&root)?;
    let end = checkpoint["root.json"]["signed"]["version"]
        .as_u64()
        .ok_or("Invalid TUF checkpoint")?;
    let start = root.signed.version.get();
    if end < start || end - start > 1_000_000 {
        return Err("Invalid checkpoint root history".into());
    }
    for version in start + 1..=end {
        let next: Signed<Root> = serde_json::from_slice(&read_limited(
            &roots.join(format!("{version}.root.json")),
            MAX_META,
        )?)?;
        if next.signed.version.get() != version {
            return Err("Checkpoint root version mismatch".into());
        }
        root.signed.verify_role(&next)?;
        next.signed.verify_role(&next)?;
        root = next;
    }
    if serde_json::to_value(&root)? != checkpoint["root.json"] {
        return Err("Checkpoint root differs from verified chain".into());
    }
    if let Some(v) = checkpoint.get("timestamp.json") {
        root.signed
            .verify_role(&serde_json::from_value::<Signed<Timestamp>>(v.clone())?)?;
    }
    if let Some(v) = checkpoint.get("snapshot.json") {
        root.signed
            .verify_role(&serde_json::from_value::<Signed<Snapshot>>(v.clone())?)?;
    }
    Ok(serde_json::to_vec(&root)?)
}
fn role_keys(root: &Value, role: &str) -> Value {
    let r = &root["signed"];
    let mut keys = BTreeMap::new();
    if let Some(ids) = r["roles"][role]["keyids"].as_array() {
        for id in ids {
            if let Some(id) = id.as_str() {
                keys.insert(id, r["keys"][id].clone());
            }
        }
    }
    json!(keys)
}
fn checkpoint_floors(prior: &Value, next: &Value) -> Result<(), Error> {
    if next["root.json"]["signed"]["version"].as_u64()
        < prior["root.json"]["signed"]["version"].as_u64()
    {
        return Err("TUF root checkpoint rollback".into());
    }
    // Compare key sets, not their serialization order. A historical replay or
    // root-key reordering must not become a rollback-floor reset.
    if ["timestamp", "snapshot"]
        .iter()
        .any(|role| role_keys(&prior["root.json"], role) != role_keys(&next["root.json"], role))
    {
        return Ok(());
    }
    for role in ["timestamp.json", "snapshot.json"] {
        if let (Some(old), Some(new)) = (prior.get(role), next.get(role)) {
            if new["signed"]["version"].as_u64() < old["signed"]["version"].as_u64() {
                return Err("TUF metadata checkpoint rollback".into());
            }
            for (name, record) in old["signed"]["meta"]
                .as_object()
                .ok_or("Invalid checkpoint meta")?
            {
                if new["signed"]["meta"][name]["version"].as_u64() < record["version"].as_u64() {
                    return Err("TUF referenced metadata checkpoint rollback".into());
                }
            }
        }
    }
    Ok(())
}
fn save_checkpoint(
    pin: &[u8],
    dir: &Path,
    datastore: &Path,
    prior: &Value,
) -> Result<Value, Error> {
    let root_path = datastore.join("root.json");
    if !root_path.exists() {
        return Ok(prior.clone());
    }
    let root = trust::read(&root_path)?;
    let rotated = ["timestamp", "snapshot"]
        .iter()
        .any(|role| role_keys(&root, role) != role_keys(&prior["root.json"], role));
    let mut value = json!({"root.json":root});
    for name in ["timestamp.json", "snapshot.json"] {
        let path = datastore.join(name);
        if path.exists() {
            let v = trust::read(&path)?;
            // Interrupted root catch-up may stop before tough clears the old
            // role metadata. Discard that old floor only across a real rotation.
            if rotated && prior.get(name) == Some(&v) {
                fs::remove_file(path)?;
            } else {
                value[name] = v;
            }
        } else if !rotated {
            if let Some(v) = prior.get(name) {
                value[name] = v.clone();
                atomic(&path, &serde_json::to_vec(v)?)?;
            }
        }
    }
    authenticate_checkpoint(pin, &dir.join("roots"), &value)?;
    checkpoint_floors(prior, &value)?;
    atomic(&dir.join("checkpoint.json"), &serde_json::to_vec(&value)?)?;
    Ok(value)
}
async fn load_catalog(
    trust: &Value,
    cache: &Path,
    dev: bool,
    cache_only: bool,
    log: &(dyn Fn(String) + Sync),
) -> Result<Catalog, Error> {
    let id = bundle::strval(&trust["repository_id"])?;
    if id.is_empty()
        || id.len() > 160
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err("Invalid TUF repository ID".into());
    }
    let root = trust::canonical(&trust["root"])?;
    let initial = trust["root"]["signed"]["version"]
        .as_u64()
        .ok_or("Missing pinned root version")?;
    if trust["root"]["signed"]["consistent_snapshot"] != true {
        return Err("Consistent TUF snapshots required".into());
    }
    let identity = bundle::sha(&trust::canonical(&json!({"id":id,"root":trust["root"]}))?);
    let dir = cache.join("tuf").join(identity);
    fs::create_dir_all(dir.join("roots"))?;
    let base = url(
        &(bundle::strval(&trust["url"])?
            .trim_end_matches('/')
            .to_owned()
            + "/"),
        dev,
    )?;
    let http = client(dev)?;
    let head_path = dir.join("current.json");
    let head = if head_path.exists() {
        Some(trust::read(&head_path)?)
    } else {
        None
    };
    let datastore = tempfile::tempdir_in(&dir)?;
    let mut previous = None;
    if let Some(head) = &head {
        let proof_store = tempfile::tempdir_in(&dir)?;
        historical(&root, &base, &dir, head, &http, proof_store.path(), initial).await?;
    }
    let checkpoint_path = dir.join("checkpoint.json");
    let mut checkpoint = if checkpoint_path.exists() {
        trust::read(&checkpoint_path)?
    } else {
        if head.is_some() {
            return Err("Missing authenticated rollback checkpoint".into());
        }
        json!({"root.json":serde_json::from_slice::<Value>(&root)?})
    };
    let mut anchor = authenticate_checkpoint(&root, &dir.join("roots"), &checkpoint)?;
    for name in ["timestamp.json", "snapshot.json"] {
        if let Some(v) = checkpoint.get(name) {
            atomic(&datastore.path().join(name), &serde_json::to_vec(v)?)?;
        }
    }
    let accepted = dir.join("accepted.json");
    if accepted.exists() {
        if head.is_none() {
            return Err("Missing TUF high-water state".into());
        }
        let old = trust::read(&accepted)?;
        let proof_store = tempfile::tempdir_in(&dir)?;
        previous =
            Some(historical(&root, &base, &dir, &old, &http, proof_store.path(), initial).await?);
    }
    let attempt = tempfile::tempdir_in(&dir)?;
    let mut use_cache = cache_only;
    let mut status = if cache_only {
        "verified-cache"
    } else {
        "verified-online"
    };
    for batch in 0..=BATCHES {
        let (responses, stop) = if use_cache {
            let h = head.as_ref().ok_or("No verified TUF cache available")?;
            if h["root_version"] != checkpoint["root.json"]["signed"]["version"] {
                return Err("Cached view predates verified root progress".into());
            }
            (
                proof_path(&dir, h)?,
                Some(
                    h["root_version"]
                        .as_u64()
                        .ok_or("Bad cached root version")?,
                ),
            )
        } else {
            (attempt.path().to_path_buf(), None)
        };
        let mut count = 0;
        while dir
            .join("roots")
            .join(format!("{}.root.json", initial + count + 1))
            .is_file()
        {
            count += 1;
            if count > 1_000_000 {
                return Err("Root history exceeds resource bound".into());
            }
        }
        let transport = CatalogTransport {
            base: base.clone(),
            roots: dir.join("roots"),
            responses: responses.clone(),
            offline: use_cache,
            stop_root: stop,
            http: http.clone(),
            progress: Arc::default(),
        };
        let result = async {
            let repo =
                RepositoryLoader::new(&anchor, base.join("metadata/")?, base.join("targets/")?)
                    .transport(transport.clone())
                    .datastore(datastore.path())
                    .limits(Limits {
                        max_root_updates: count + ROOT_BATCH as u64 + 2,
                        ..Limits::default()
                    })
                    .load()
                    .await?;
            if !repo.root().signed.consistent_snapshot {
                return Err("Consistent TUF snapshots required".into());
            }
            let data = repo
                .read_target(&TargetName::new("catalog.json")?)
                .await?
                .ok_or("Missing catalog")?
                .into_vec()
                .await?;
            if data.len() as u64 > MAX_CATALOG {
                return Err("Oversized catalog".into());
            }
            Ok::<_, Error>((data, repo.root().signed.version.get()))
        }
        .await;
        checkpoint = save_checkpoint(&root, &dir, datastore.path(), &checkpoint)?;
        anchor = authenticate_checkpoint(&root, &dir.join("roots"), &checkpoint)?;
        match result {
            Ok((data, root_version)) => {
                let value = serde_json::from_slice(&data)?;
                let mut new_head = head.clone().unwrap_or(Value::Null);
                if !use_cache {
                    let generation = bundle::sha(&data);
                    let destination = dir.join("generations").join(&generation);
                    // The catalog may be unchanged while TUF metadata renews.
                    // A unique proof ID binds its full metadata response set.
                    let mut files = BTreeMap::new();
                    collect_files(&responses, &responses, &mut files)?;
                    let proof_id = bundle::sha(&trust::canonical(&json!(files))?);
                    let destination = destination.join(&proof_id);
                    fs::create_dir_all(&destination)?;
                    for name in files.keys() {
                        let dest = destination.join(name);
                        atomic(&dest, &fs::read(responses.join(name))?)?;
                    }
                    // Store an explicit proof path; catalog hash remains the generation identity.
                    new_head = json!({"generation":generation,"proof":proof_id,"root_version":root_version});
                    atomic(&dir.join("current.json"), &serde_json::to_vec(&new_head)?)?;
                }
                return Ok(Catalog {
                    value,
                    previous,
                    status,
                    root_version,
                    accepted,
                    head: new_head,
                });
            }
            Err(e) => {
                let failure = transport.progress.lock().unwrap().failure;
                if !use_cache && failure == Some(Failure::Pending) {
                    if batch == BATCHES - 1 {
                        return Err(
                            "root-update-pending: progress retained; retry the operation".into(),
                        );
                    }
                    log(format!("{id}: verified another root-history batch…"));
                    continue;
                }
                if !use_cache && failure == Some(Failure::Unavailable) && head.is_some() {
                    log(format!("{id}: network unavailable; re-verifying cached metadata at the current clock."));
                    use_cache = true;
                    status = "verified-cache";
                    continue;
                }
                return Err(describe(e.as_ref()).into());
            }
        }
    }
    Err("root-update-pending".into())
}
fn collect_files(root: &Path, at: &Path, out: &mut BTreeMap<String, String>) -> Result<(), Error> {
    for entry in fs::read_dir(at)? {
        let entry = entry?;
        if entry.file_type()?.is_symlink() {
            return Err("Symlink in TUF cache".into());
        }
        if entry.file_type()?.is_dir() {
            collect_files(root, &entry.path(), out)?;
        } else {
            out.insert(
                entry
                    .path()
                    .strip_prefix(root)?
                    .to_string_lossy()
                    .replace('\\', "/"),
                bundle::file_hash(&entry.path())?,
            );
        }
    }
    Ok(())
}
fn registry_record<'a>(
    record: &'a Value,
    source: &Value,
    current: bool,
) -> Result<&'a Value, Error> {
    let r = &record["signed"];
    let reg = &r["registry"];
    if record.as_object().ok_or("Registry envelope")?.len() != 2
        || !record["signature"].is_string()
        || r["kind"] != "registry"
        || r["schema"] != 1
        || r["registry_id"] != source["registry_id"]
        || reg["schema"] != 1
        || reg["online_trust"] != "tuf"
    {
        return Err("Invalid TUF registry identity/schema".into());
    }
    if r["revision"].as_u64().unwrap_or(0) < source["minimum_revision"].as_u64().unwrap_or(1) {
        return Err("Registry revision below bootstrap floor".into());
    }
    for field in ["season", "channel", "target"] {
        if reg[field] != source[field] {
            return Err("Online registry scope mismatch".into());
        }
    }
    let issued = r["issued_at"].as_u64().ok_or("Invalid registry issuance")?;
    let expires = r["expires_at"].as_u64().ok_or("Invalid registry expiry")?;
    if issued == 0 || expires <= issued || (current && (issued > now()? || expires <= now()?)) {
        return Err("Registry online authorization expired or not yet valid".into());
    }
    Ok(reg)
}
fn monotonic(new: &Value, old: Option<&Value>) -> Result<(), Error> {
    if let Some(old) = old {
        let n = new["signed"]["revision"]
            .as_u64()
            .ok_or("Invalid registry revision")?;
        let o = old["signed"]["revision"]
            .as_u64()
            .ok_or("Invalid historical revision")?;
        if n < o || n == o && new != old {
            return Err("Registry rollback or same-revision mutation rejected".into());
        }
    }
    Ok(())
}
fn publisher_order(reg: &Value) -> Result<Vec<String>, Error> {
    let publishers = reg["repositories"]
        .as_object()
        .ok_or("Missing publishers")?;
    if publishers.is_empty() || publishers.len() > 64 {
        return Err("Invalid publisher count".into());
    }
    let order = if let Some(a) = reg["repository_order"].as_array() {
        a.iter()
            .map(|v| bundle::strval(v).map(str::to_owned))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        publishers.keys().cloned().collect()
    };
    if order.len() != publishers.len()
        || order.iter().collect::<BTreeSet<_>>() != publishers.keys().collect()
    {
        return Err("Invalid publisher order".into());
    }
    Ok(order)
}
fn releases(catalog: &Value, reg: &Value, publisher: &str) -> Result<Vec<Value>, Error> {
    if catalog["kind"] != "publisher-catalog"
        || catalog["schema"] != 1
        || catalog["publisher"] != publisher
    {
        return Err("Publisher catalog identity mismatch".into());
    }
    for f in ["season", "channel", "target"] {
        if catalog[f] != reg[f] {
            return Err("Publisher catalog scope mismatch".into());
        }
    }
    let all = catalog["releases"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 8)
        .ok_or("Invalid retained release history")?;
    let policy = &reg["repositories"][publisher];
    let mut previous = 0;
    let mut result = vec![];
    for envelope in all {
        let m = trust::signed(envelope, &policy["public_key"])?;
        let revision = m["revision"].as_u64().ok_or("Invalid release revision")?;
        if revision <= previous || m["schema"] != 1 || m["publisher"] != publisher {
            return Err("Invalid publisher revision history".into());
        }
        previous = revision;
        for f in ["season", "channel", "target"] {
            if m[f] != reg[f] {
                return Err("Release scope mismatch".into());
            }
        }
        for (name, record) in m["files"].as_object().ok_or("Missing release files")? {
            bundle::safe(name)?;
            if !hex(bundle::strval(&record["sha256"])?) || record["size"].as_u64().is_none() {
                return Err("Invalid release file record".into());
            }
        }
        if revision >= policy["minimum_revision"].as_u64().unwrap_or(1) {
            result.push(envelope.clone())
        }
    }
    if result.is_empty() {
        return Err("No publisher release meets registry floor".into());
    }
    result.reverse();
    Ok(result)
}
fn choose(
    order: &[String],
    available: &BTreeMap<String, Vec<Value>>,
) -> Result<BTreeMap<String, Value>, Error> {
    let mut indices = vec![0usize; order.len()];
    for _ in 0..4096 {
        let mut packages = BTreeMap::new();
        let mut valid = true;
        for (i, name) in order.iter().enumerate() {
            let release = &available[name][indices[i]]["signed"];
            for p in release["packages"].as_array().ok_or("Missing packages")? {
                if packages
                    .insert(bundle::strval(&p["id"])?.to_owned(), p.clone())
                    .is_some()
                {
                    valid = false;
                }
            }
        }
        if valid && bundle::validate_plan(&packages).is_ok() {
            return Ok(order
                .iter()
                .enumerate()
                .map(|(i, n)| (n.clone(), available[n][indices[i]].clone()))
                .collect());
        }
        let mut carry = true;
        for i in (0..indices.len()).rev() {
            indices[i] += 1;
            if indices[i] < available[&order[i]].len() {
                carry = false;
                break;
            }
            indices[i] = 0;
        }
        if carry {
            break;
        }
    }
    Err("No compatible complete repository set within selection budget".into())
}
async fn verified_file(
    http: &reqwest::Client,
    base: &str,
    name: &str,
    record: &Value,
    cache: &Path,
    dest: &Path,
    dev: bool,
    cache_only: bool,
) -> Result<(), Error> {
    bundle::safe(name)?;
    let hash = bundle::strval(&record["sha256"])?;
    if !hex(hash) {
        return Err("Invalid payload hash".into());
    }
    let size = record["size"]
        .as_u64()
        .filter(|s| *s <= 32 * 1024 * 1024 * 1024)
        .ok_or("Invalid payload size")?;
    let cached = cache.join("objects").join(hash);
    if cached.exists() {
        if fs::symlink_metadata(&cached)?.file_type().is_symlink()
            || fs::metadata(&cached)?.len() != size
            || bundle::file_hash(&cached)? != hash
        {
            return Err("Corrupt verified payload cache".into());
        }
    } else {
        if cache_only {
            return Err(format!("Verified payload is not cached: {name}").into());
        }
        let base = url(&(base.trim_end_matches('/').to_owned() + "/"), dev)?;
        let u = base.join(name)?;
        let mut response = http.get(u).send().await?;
        if !response.status().is_success() {
            return Err(format!("Payload HTTP {}: {name}", response.status()).into());
        }
        if response.content_length().is_some_and(|n| n != size) {
            return Err("Payload size mismatch".into());
        }
        fs::create_dir_all(cached.parent().unwrap())?;
        let mut f = tempfile::NamedTempFile::new_in(cached.parent().unwrap())?;
        let mut n = 0;
        while let Some(chunk) = response.chunk().await? {
            n += chunk.len() as u64;
            if n > size {
                return Err("Oversized package download".into());
            }
            f.write_all(&chunk)?;
        }
        if n != size || bundle::file_hash(f.path())? != hash {
            return Err(format!("Package hash mismatch: {name}").into());
        }
        f.as_file().sync_all()?;
        f.persist(&cached)?;
    }
    fs::create_dir_all(dest.parent().ok_or("Missing payload parent")?)?;
    fs::copy(cached, dest)?;
    // Authenticate the private copy consumed by Qt, closing a cache replacement
    // race between verification and copying into the worker workspace.
    if fs::metadata(dest)?.len() != size || bundle::file_hash(dest)? != hash {
        return Err("Payload cache changed during verification".into());
    }
    Ok(())
}
pub fn load(
    source: &Value,
    cache: &Path,
    cache_only: bool,
    log: &(dyn Fn(String) + Sync),
) -> Result<Bundle, Error> {
    tokio::runtime::Runtime::new()?.block_on(load_async(source, cache, cache_only, log))
}
async fn load_async(
    source: &Value,
    cache: &Path,
    cache_only: bool,
    log: &(dyn Fn(String) + Sync),
) -> Result<Bundle, Error> {
    let dev = source["allow_loopback_http"] == true;
    let http = client(dev)?;
    log("Refreshing the registry through TUF…".into());
    let catalog = load_catalog(&source["tuf"], cache, dev, cache_only, log).await?;
    let reg = registry_record(&catalog.value, source, true)?.clone();
    if let Some(old) = &catalog.previous {
        registry_record(old, source, false)?;
    }
    monotonic(&catalog.value, catalog.previous.as_ref())?;
    let order = publisher_order(&reg)?;
    let mut tuf_ids = BTreeSet::new();
    tuf_ids.insert(bundle::strval(&source["tuf"]["repository_id"])?.to_owned());
    let mut available = BTreeMap::new();
    let mut statuses = vec![
        json!({"repository":"registry","status":catalog.status,"root_version":catalog.root_version}),
    ];
    let mut publisher_catalogs = vec![];
    for name in &order {
        bundle::safe(name)?;
        if name.contains('/')
            || !name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
        {
            return Err("Invalid publisher identifier".into());
        }
        let policy = &reg["repositories"][name];
        url(bundle::strval(&policy["url"])?, dev)?;
        if !tuf_ids.insert(bundle::strval(&policy["tuf"]["repository_id"])?.to_owned()) {
            return Err("Publishers must have independent TUF identities".into());
        }
        log(format!("Refreshing {name} through its own TUF root…"));
        let c = load_catalog(&policy["tuf"], cache, dev, cache_only, log).await?;
        let candidates = releases(&c.value, &reg, name)?;
        // TUF version rollback is handled by tough; also reject a newer TUF
        // catalog that lowers/repackages its highest publisher revision.
        if let Some(old) = &c.previous {
            let old_all = old["releases"]
                .as_array()
                .ok_or("Invalid prior publisher catalog")?;
            let prior = old_all.last().ok_or("Empty prior history")?;
            let latest = &c.value["releases"].as_array().unwrap().last().unwrap();
            let a = prior["signed"]["revision"]
                .as_u64()
                .ok_or("Invalid prior revision")?;
            let b = latest["signed"]["revision"]
                .as_u64()
                .ok_or("Invalid revision")?;
            if b < a || b == a && *latest != prior {
                return Err("Publisher revision rollback or mutation".into());
            }
        }
        statuses.push(json!({"repository":name,"status":c.status,"root_version":c.root_version}));
        available.insert(name.clone(), candidates);
        publisher_catalogs.push(c);
    }
    let selected = choose(&order, &available)?;
    let temp = tempfile::tempdir()?;
    let root = temp.path().to_path_buf();
    let mut references = serde_json::Map::new();
    let mut remote = serde_json::Map::new();
    let mut held = vec![];
    for (name, envelope) in &selected {
        let repo = root.join("repos").join(name);
        fs::create_dir_all(&repo)?;
        atomic(&repo.join("release.json"), &serde_json::to_vec(envelope)?)?;
        let m = &envelope["signed"];
        references.insert(
            name.clone(),
            json!({"revision":m["revision"],"sha256":bundle::sha(&trust::canonical(envelope)?)}),
        );
        let base = format!(
            "{}/releases/{}",
            bundle::strval(&reg["repositories"][name]["url"])?.trim_end_matches('/'),
            m["revision"]
        );
        verified_file(
            &http,
            &base,
            "Updates.xml",
            &m["files"]["Updates.xml"],
            cache,
            &repo.join("Updates.xml"),
            dev,
            cache_only,
        )
        .await?;
        remote.insert(name.clone(), json!({"url":base,"files":m["files"]}));
        if m["revision"] != available[name][0]["signed"]["revision"] {
            held.push(json!({"publisher":name,"selected":m["revision"],"latest":available[name][0]["signed"]["revision"]}));
        }
    }
    let identity = json!({"registry_id":source["registry_id"],"registry_revision":catalog.value["signed"]["revision"],"registry_sha256":bundle::sha(&trust::canonical(&catalog.value)?),"status":if statuses.iter().any(|s|s["status"]=="verified-cache"){"verified-cache"}else{"verified-online"},"repositories":statuses,"held_back":held});
    let mut bundle = Bundle::repositories(
        temp,
        &root,
        reg,
        identity,
        &Value::Object(references),
        true,
        log,
    )?;
    bundle.remote = Some(Value::Object(remote));
    catalog.accept()?;
    for c in publisher_catalogs {
        c.accept()?;
    }
    Ok(bundle)
}
pub fn materialize(
    bundle: &Bundle,
    selected: &BTreeSet<String>,
    cache: &Path,
    source: &Value,
    cache_only: bool,
    log: &(dyn Fn(String) + Sync),
) -> Result<(), Error> {
    tokio::runtime::Runtime::new()?.block_on(async {
        let dev = source["allow_loopback_http"] == true;
        let http = client(dev)?;
        let remote = bundle
            .remote
            .as_ref()
            .ok_or("No online repositories")?
            .as_object()
            .ok_or("Invalid online repositories")?;
        for (publisher, repo) in remote {
            let path = bundle
                .repos
                .iter()
                .find(|p| p.file_name().is_some_and(|n| n == publisher.as_str()))
                .ok_or("Missing private publisher repository")?;
            let xml = fs::read_to_string(path.join("Updates.xml"))?;
            let doc = roxmltree::Document::parse(&xml)?;
            let mut payloads = BTreeMap::new();
            for node in doc
                .root_element()
                .children()
                .filter(|n| n.has_tag_name("PackageUpdate"))
            {
                let value = |tag| {
                    node.children()
                        .find(|n| n.has_tag_name(tag))
                        .and_then(|n| n.text())
                        .unwrap_or("")
                };
                let id = value("Name");
                let version = value("Version");
                for archive in value("DownloadableArchives")
                    .split(',')
                    .filter(|s| !s.is_empty())
                {
                    payloads.insert(format!("{id}/{version}{archive}"), id.to_owned());
                }
            }
            for (name, record) in repo["files"].as_object().ok_or("Missing files")? {
                if payloads.get(name).is_some_and(|id| !selected.contains(id)) {
                    continue;
                }
                log(format!("Verifying/download {publisher}/{name}…"));
                verified_file(
                    &http,
                    bundle::strval(&repo["url"])?,
                    name,
                    record,
                    cache,
                    &path.join(name),
                    dev,
                    cache_only,
                )
                .await?;
            }
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn production_urls_cannot_bypass_https() {
        assert!(url("https://example.test/repo", false).is_ok());
        for u in [
            "http://example.test/repo",
            "http://127.0.0.1/repo",
            "file:///tmp/repo",
            "https://user:secret@example.test/repo",
            "https://example.test/repo?token=x",
            "https://example.test/repo#fragment",
        ] {
            assert!(url(u, false).is_err(), "{u}");
        }
        assert!(url("http://127.0.0.1:18765/repo", true).is_ok());
        assert!(url("http://example.test/repo", true).is_err());
    }
    #[test]
    fn root_renewal_cannot_reset_metadata_floors() {
        let prior = json!({"root.json":{"signed":{"version":1,"keys":{},"roles":{}}},
            "timestamp.json":{"signed":{"version":7,"meta":{"snapshot.json":{"version":5}}}}});
        let mut next = prior.clone();
        next["root.json"]["signed"]["version"] = json!(2);
        assert!(checkpoint_floors(&prior, &next).is_ok());
        next["timestamp.json"]["signed"]["version"] = json!(6);
        assert!(checkpoint_floors(&prior, &next).is_err());
        next["timestamp.json"]["signed"]["version"] = json!(8);
        next["timestamp.json"]["signed"]["meta"]["snapshot.json"]["version"] = json!(4);
        assert!(checkpoint_floors(&prior, &next).is_err());
    }
    #[test]
    fn registry_revisions_are_immutable() {
        let old = json!({"signed":{"revision":7,"registry":{"profiles":{}}}});
        assert!(monotonic(&old, Some(&old)).is_ok());
        assert!(monotonic(&json!({"signed":{"revision":6}}), Some(&old)).is_err());
        assert!(monotonic(&json!({"signed":{"revision":7}}), Some(&old)).is_err());
        assert!(monotonic(&json!({"signed":{"revision":8}}), Some(&old)).is_ok());
    }
}
