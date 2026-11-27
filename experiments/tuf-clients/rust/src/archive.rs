// Limited archival authorization probe, not the complete bundle loader.
use aws_lc_rs::{digest, signature};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{fs, path::Path};
type Error = Box<dyn std::error::Error>;
fn canonical(v: &Value) -> Result<Vec<u8>, Error> {
    fn encode(v: &Value, s: &mut String) -> Result<(), Error> {
        match v {
            Value::Null => s.push_str("null"),
            Value::Bool(b) => s.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => {
                if !n.is_i64() && !n.is_u64() {
                    return Err("noninteger archival number".into());
                }
                s.push_str(&n.to_string());
            }
            Value::String(v) => {
                s.push('"');
                for c in v.chars() {
                    match c {
                        '"' => s.push_str("\\\""),
                        '\\' => s.push_str("\\\\"),
                        '\n' => s.push_str("\\n"),
                        '\r' => s.push_str("\\r"),
                        '\t' => s.push_str("\\t"),
                        '\u{08}' => s.push_str("\\b"),
                        '\u{0c}' => s.push_str("\\f"),
                        c if !(32..=126).contains(&(c as u32)) => {
                            for u in c.encode_utf16(&mut [0; 2]) {
                                s.push_str(&format!("\\u{u:04x}"));
                            }
                        }
                        c => s.push(c),
                    }
                }
                s.push('"');
            }
            Value::Array(a) => {
                s.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        s.push(',');
                    }
                    encode(v, s)?;
                }
                s.push(']');
            }
            Value::Object(m) => {
                s.push('{');
                let mut keys: Vec<_> = m.keys().collect();
                keys.sort();
                for (i, k) in keys.iter().enumerate() {
                    if i > 0 {
                        s.push(',');
                    }
                    encode(&Value::String((*k).clone()), s)?;
                    s.push(':');
                    encode(&m[*k], s)?;
                }
                s.push('}');
            }
        };
        Ok(())
    }
    let mut s = String::new();
    encode(v, &mut s)?;
    Ok(s.into_bytes())
}
fn signed<'a>(envelope: &'a Value, key: &Value) -> Result<&'a Value, Error> {
    if envelope.as_object().ok_or("expected envelope")?.len() != 2 {
        return Err("invalid envelope".into());
    }
    let key = STANDARD.decode(key.as_str().ok_or("invalid key")?)?;
    let sig = STANDARD.decode(envelope["signature"].as_str().ok_or("invalid signature")?)?;
    let data = canonical(envelope.get("signed").ok_or("missing signed payload")?)?;
    signature::UnparsedPublicKey::new(&signature::ED25519, key)
        .verify(&data, &sig)
        .map_err(|_| "invalid archival signature")?;
    if !envelope["signed"].is_object() {
        return Err("expected signed object".into());
    }
    Ok(&envelope["signed"])
}
fn read(path: impl AsRef<Path>) -> Result<Value, Error> {
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
pub fn probe(source: &str, bundle: &str) -> Result<Value, Error> {
    let source = read(source)?;
    let bundle = Path::new(bundle);
    let record = read(bundle.join("registry.json"))?;
    let r = signed(&record, &source["public_key"])?;
    let reg = &r["registry"];
    let policy = json!({"name":"wpilib-archive-v2","lifetime":"indefinite"});
    if source["archive_policy"] != policy
        || reg["archive_policy"] != policy
        || reg["online_trust"] != "tuf"
    {
        return Err("unsupported archival policy".into());
    }
    if r["kind"] != "registry" || r["schema"] != 1 || r["registry_id"] != source["registry_id"] {
        return Err("registry identity mismatch".into());
    }
    for f in ["season", "channel", "target"] {
        if reg[f] != source[f] {
            return Err("scope mismatch".into());
        }
    }
    if r["revision"].as_u64().unwrap_or(0) == 0 || r["issued_at"].as_u64().unwrap_or(0) == 0 {
        return Err("invalid revision/issuance".into());
    }
    if r.get("offline_expires_at") != Some(&Value::Null) {
        return Err("archival expiry".into());
    }
    let envelope = read(bundle.join("bundle-manifest.json"))?;
    let b = signed(&envelope, &reg["bundle_public_key"])?;
    let hash = digest::digest(&digest::SHA256, &canonical(&record)?)
        .as_ref()
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect::<String>();
    let identity = json!({"registry_id":r["registry_id"],"revision":r["revision"],"sha256":hash});
    if b["schema"] != 3 || b["registry"] != identity {
        return Err("bundle registry mismatch".into());
    }
    let a = &b["archival"];
    if a["policy"] != "wpilib-archive-v2"
        || a["issued_at"] != r["issued_at"]
        || a.get("expires_at") != Some(&Value::Null)
        || a.get("not_before").is_some()
    {
        return Err("invalid archival authorization".into());
    }
    let repos = reg["repositories"].as_object().ok_or("repositories")?;
    let pubs = a["publishers_tuf"]
        .as_object()
        .ok_or("publisher evidence")?;
    if repos.len() != pubs.len() {
        return Err("publisher set mismatch".into());
    }
    let mut evidence = vec![&a["registry_tuf"]];
    for (name, repo) in repos {
        let p = pubs.get(name).ok_or("missing publisher")?;
        if p["repository_id"] != repo["tuf"]["repository_id"] {
            return Err("publisher identity mismatch".into());
        }
        evidence.push(p);
    }
    for e in evidence {
        if e["repository_id"].as_str().unwrap_or("").is_empty() {
            return Err("provenance identity".into());
        }
        let h = e["catalog_sha256"].as_str().ok_or("provenance hash")?;
        if h.len() != 64
            || !h
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("provenance hash".into());
        }
    }
    Ok(json!({"status":"ok","registry_revision":r["revision"],"registry_sha256":hash}))
}
