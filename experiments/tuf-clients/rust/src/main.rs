// Experimental client only: fixture URLs and an untrusted response cache.
mod archive;
use async_trait::async_trait;
use serde_json::{json, Value};
use std::{
    env, fs,
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tough::{
    IntoVec, Limits, RepositoryLoader, TargetName, Transport, TransportError, TransportErrorKind,
    TransportStream,
};
use url::Url;

#[derive(Debug, Default)]
struct Progress {
    fetched: usize,
    complete: bool,
}
#[derive(Debug, Clone)]
struct ProbeTransport {
    origin: PathBuf,
    responses: PathBuf,
    mode: String,
    budget: usize,
    progress: Arc<Mutex<Progress>>,
}
impl ProbeTransport {
    fn read(&self, url: &Url) -> Result<Vec<u8>, TransportError> {
        let fail = |msg: &str| {
            TransportError::new_with_cause(
                TransportErrorKind::Other,
                url.as_str(),
                std::io::Error::other(msg.to_string()),
            )
        };
        let missing = || TransportError::new(TransportErrorKind::FileNotFound, url.as_str());
        let name = url
            .as_str()
            .strip_prefix("https://fixture.invalid/")
            .ok_or_else(|| fail("unexpected URL"))?;
        if name.contains("..") || name.contains(['\\', ':', '%', '?', '#']) {
            return Err(fail("unsafe fixture path"));
        }
        let root = name.ends_with(".root.json");
        let path = self.responses.join(name);
        let mut progress = self.progress.lock().unwrap();
        if self.mode == "cache" || (root && self.budget > 0) {
            if let Ok(data) = fs::read(&path) {
                return Ok(data);
            }
        }
        if root && self.budget > 0 && progress.fetched >= self.budget {
            return Err(fail("root-update-pending"));
        }
        if !root && self.budget > 0 && !progress.complete {
            return Err(fail("root-update-pending: no end-of-chain"));
        }
        if self.mode == "cache" {
            if root {
                progress.complete = true;
                return Err(missing());
            }
            return Err(fail("cache miss"));
        }
        let data = match fs::read(self.origin.join(name)) {
            Ok(data) => data,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if root {
                    progress.complete = true;
                }
                return Err(missing());
            }
            Err(e) => return Err(fail(&e.to_string())),
        };
        if data.len() > 16 * 1024 * 1024 {
            return Err(fail("oversized fixture response"));
        }
        if root {
            progress.fetched += 1;
        }
        // Untrusted bytes: the library replays signatures from the ORIGINAL pin.
        fs::create_dir_all(path.parent().unwrap()).map_err(|e| fail(&e.to_string()))?;
        fs::write(path, &data).map_err(|e| fail(&e.to_string()))?;
        Ok(data)
    }
}
#[async_trait]
impl Transport for ProbeTransport {
    async fn fetch(&self, url: Url) -> Result<TransportStream, TransportError> {
        // tough 0.24 treats an initial fetch Err as end-of-root-chain. Deliver
        // non-missing transport failures in the stream so they remain failures.
        // raw mode exists solely to record the unadapted library behavior.
        match self.read(&url) {
            Ok(data) => Ok(Box::pin(futures::stream::once(
                async move { Ok(data.into()) },
            ))),
            Err(error)
                if error.kind() == TransportErrorKind::FileNotFound || self.mode == "raw" =>
            {
                Err(error)
            }
            Err(error) => Ok(Box::pin(futures::stream::once(async move { Err(error) }))),
        }
    }
}
async fn probe() -> Result<Value, Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 7 {
        return Err("usage: probe ROOT ORIGIN STATE live|cache ROOT-LIMIT ROOT-BUDGET".into());
    }
    if args[4] == "archive" {
        return archive::probe(&args[1], &args[2]);
    }
    let root = fs::read(&args[1])?;
    let state = PathBuf::from(&args[3]);
    let limit: u64 = args[5].parse()?;
    let budget: usize = args[6].parse()?;
    if args[4] != "live" && args[4] != "cache" && args[4] != "raw" {
        return Err("invalid mode".into());
    }
    fs::create_dir_all(state.join("metadata"))?;
    let transport = ProbeTransport {
        origin: args[2].clone().into(),
        responses: state.join("responses"),
        mode: args[4].clone(),
        budget,
        progress: Arc::default(),
    };
    let mut loader = RepositoryLoader::new(
        &root,
        Url::parse("https://fixture.invalid/metadata/")?,
        Url::parse("https://fixture.invalid/targets/")?,
    )
    .datastore(state.join("metadata"))
    .transport(transport.clone());
    if limit > 0 {
        loader = loader.limits(Limits {
            max_root_updates: limit,
            ..Limits::default()
        });
    }
    let repo = loader.load().await?;
    let stream = repo
        .read_target(&TargetName::new("catalog.json")?)
        .await?
        .ok_or("missing catalog")?;
    let data = stream.into_vec().await?;
    if data.len() > 8 * 1024 * 1024 {
        return Err("oversized catalog".into());
    }
    let catalog: Value = serde_json::from_slice(&data)?;
    let fetched = transport.progress.lock().unwrap().fetched;
    Ok(
        json!({"status":"ok","catalog":catalog,"root_version":repo.root().signed.version.get(),"new_roots":fetched}),
    )
}
#[tokio::main]
async fn main() {
    match probe().await {
        Ok(value) => println!("{value}"),
        Err(error) => {
            let mut messages = vec![error.to_string()];
            let mut source = error.source();
            while let Some(cause) = source {
                messages.push(cause.to_string());
                source = cause.source();
            }
            println!(
                "{}",
                json!({"status":"error","error":messages.join(" <- ")})
            );
            std::process::exit(1);
        }
    }
}
