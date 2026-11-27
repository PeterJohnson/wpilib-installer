package main

// Limited archival authorization probe, not the complete bundle loader.
import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"unicode/utf16"
)

func readJSON(path string) (any, error) {
	b, e := os.ReadFile(path)
	if e != nil {
		return nil, e
	}
	var v any
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	e = d.Decode(&v)
	return v, e
}
func canonical(v any) ([]byte, error) {
	switch x := v.(type) {
	case nil:
		return []byte("null"), nil
	case bool:
		if x {
			return []byte("true"), nil
		}
		return []byte("false"), nil
	case json.Number:
		n, ok := new(big.Int).SetString(string(x), 10)
		if !ok {
			return nil, fmt.Errorf("noninteger archival number")
		}
		return []byte(n.String()), nil
	case string:
		var b strings.Builder
		b.WriteByte('"')
		for _, r := range x {
			switch r {
			case '"':
				b.WriteString(`\"`)
			case '\\':
				b.WriteString(`\\`)
			case '\b':
				b.WriteString(`\b`)
			case '\f':
				b.WriteString(`\f`)
			case '\n':
				b.WriteString(`\n`)
			case '\r':
				b.WriteString(`\r`)
			case '\t':
				b.WriteString(`\t`)
			default:
				if r < 32 || r > 126 {
					if r > 0xffff {
						a, c := utf16.EncodeRune(r)
						fmt.Fprintf(&b, `\u%04x\u%04x`, a, c)
					} else {
						fmt.Fprintf(&b, `\u%04x`, r)
					}
				} else {
					b.WriteRune(r)
				}
			}
		}
		b.WriteByte('"')
		return []byte(b.String()), nil
	case []any:
		parts := []string{}
		for _, item := range x {
			b, e := canonical(item)
			if e != nil {
				return nil, e
			}
			parts = append(parts, string(b))
		}
		return []byte("[" + strings.Join(parts, ",") + "]"), nil
	case map[string]any:
		keys := []string{}
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := []string{}
		for _, k := range keys {
			a, _ := canonical(k)
			b, e := canonical(x[k])
			if e != nil {
				return nil, e
			}
			parts = append(parts, string(a)+":"+string(b))
		}
		return []byte("{" + strings.Join(parts, ",") + "}"), nil
	}
	return nil, fmt.Errorf("unsupported JSON type")
}
func object(v any) (map[string]any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("expected object")
	}
	return m, nil
}
func signed(v any, key any) (map[string]any, error) {
	m, e := object(v)
	if e != nil {
		return nil, e
	}
	if len(m) != 2 {
		return nil, fmt.Errorf("invalid envelope")
	}
	k, ok := key.(string)
	if !ok {
		return nil, fmt.Errorf("invalid key")
	}
	pub, e := base64.StdEncoding.Strict().DecodeString(k)
	if e != nil || len(pub) != 32 {
		return nil, fmt.Errorf("invalid key")
	}
	sigstr, ok := m["signature"].(string)
	if !ok {
		return nil, fmt.Errorf("invalid signature")
	}
	sig, e := base64.StdEncoding.Strict().DecodeString(sigstr)
	if e != nil {
		return nil, e
	}
	data, e := canonical(m["signed"])
	if e != nil {
		return nil, e
	}
	if !ed25519.Verify(pub, data, sig) {
		return nil, fmt.Errorf("invalid archival signature")
	}
	return object(m["signed"])
}
func archive(sourcePath, bundle string) (map[string]any, error) {
	sv, e := readJSON(sourcePath)
	if e != nil {
		return nil, e
	}
	source, e := object(sv)
	if e != nil {
		return nil, e
	}
	record, e := readJSON(filepath.Join(bundle, "registry.json"))
	if e != nil {
		return nil, e
	}
	r, e := signed(record, source["public_key"])
	if e != nil {
		return nil, e
	}
	reg, e := object(r["registry"])
	if e != nil {
		return nil, e
	}
	policy := map[string]any{"name": "wpilib-archive-v2", "lifetime": "indefinite"}
	if !reflect.DeepEqual(source["archive_policy"], policy) || !reflect.DeepEqual(reg["archive_policy"], policy) || reg["online_trust"] != "tuf" {
		return nil, fmt.Errorf("unsupported archival policy")
	}
	if r["kind"] != "registry" || r["schema"] != json.Number("1") || r["registry_id"] != source["registry_id"] {
		return nil, fmt.Errorf("registry identity mismatch")
	}
	for _, f := range []string{"season", "channel", "target"} {
		if reg[f] != source[f] {
			return nil, fmt.Errorf("scope mismatch")
		}
	}
	revision, ok := r["revision"].(json.Number)
	if !ok {
		return nil, fmt.Errorf("revision")
	}
	n, err := revision.Int64()
	if err != nil || n < 1 {
		return nil, fmt.Errorf("revision")
	}
	issued, ok := r["issued_at"].(json.Number)
	if !ok {
		return nil, fmt.Errorf("issuance")
	}
	n, err = issued.Int64()
	if err != nil || n < 1 {
		return nil, fmt.Errorf("issuance")
	}
	expiry, exists := r["offline_expires_at"]
	if !exists || expiry != nil {
		return nil, fmt.Errorf("archival expiry")
	}
	bv, e := readJSON(filepath.Join(bundle, "bundle-manifest.json"))
	if e != nil {
		return nil, e
	}
	b, e := signed(bv, reg["bundle_public_key"])
	if e != nil {
		return nil, e
	}
	data, e := canonical(record)
	if e != nil {
		return nil, e
	}
	hash := sha256.Sum256(data)
	identity := map[string]any{"registry_id": r["registry_id"], "revision": r["revision"], "sha256": hex.EncodeToString(hash[:])}
	if b["schema"] != json.Number("3") || !reflect.DeepEqual(b["registry"], identity) {
		return nil, fmt.Errorf("bundle registry mismatch")
	}
	a, e := object(b["archival"])
	if e != nil {
		return nil, e
	}
	expires, exists := a["expires_at"]
	_, bounded := a["not_before"]
	if a["policy"] != "wpilib-archive-v2" || a["issued_at"] != issued || !exists || expires != nil || bounded {
		return nil, fmt.Errorf("invalid archival authorization")
	}
	repos, e := object(reg["repositories"])
	if e != nil {
		return nil, e
	}
	pubs, e := object(a["publishers_tuf"])
	if e != nil {
		return nil, e
	}
	if len(repos) != len(pubs) {
		return nil, fmt.Errorf("publisher set mismatch")
	}
	evidence := []any{a["registry_tuf"]}
	for name, rv := range repos {
		repo, e := object(rv)
		if e != nil {
			return nil, e
		}
		trust, e := object(repo["tuf"])
		if e != nil {
			return nil, e
		}
		p, e := object(pubs[name])
		if e != nil {
			return nil, e
		}
		if p["repository_id"] != trust["repository_id"] {
			return nil, fmt.Errorf("publisher identity mismatch")
		}
		evidence = append(evidence, p)
	}
	for _, v := range evidence {
		m, e := object(v)
		if e != nil {
			return nil, e
		}
		id, ok := m["repository_id"].(string)
		if !ok || id == "" {
			return nil, fmt.Errorf("provenance identity")
		}
		hash, ok := m["catalog_sha256"].(string)
		if !ok {
			return nil, fmt.Errorf("provenance hash")
		}
		raw, e := hex.DecodeString(hash)
		if e != nil || len(raw) != 32 || hash != strings.ToLower(hash) {
			return nil, fmt.Errorf("provenance hash")
		}
	}
	return map[string]any{"status": "ok", "registry_revision": r["revision"], "registry_sha256": identity["sha256"]}, nil
}
