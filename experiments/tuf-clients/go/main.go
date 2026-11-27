// Experimental client only: fixture URLs and an untrusted response cache.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/theupdateframework/go-tuf/v2/metadata"
	"github.com/theupdateframework/go-tuf/v2/metadata/config"
	"github.com/theupdateframework/go-tuf/v2/metadata/updater"
)

type probeFetcher struct {
	origin, responses, mode string
	budget, fetched         int
	complete                bool
}

func (f *probeFetcher) DownloadFile(url string, max int64, _ time.Duration) ([]byte, error) {
	const prefix = "https://fixture.invalid/"
	if !strings.HasPrefix(url, prefix) {
		return nil, fmt.Errorf("unexpected URL")
	}
	name := strings.TrimPrefix(url, prefix)
	if strings.Contains(name, "..") || strings.ContainsAny(name, "\\:%?#") {
		return nil, fmt.Errorf("unsafe fixture path")
	}
	root := strings.HasSuffix(name, ".root.json")
	path := filepath.Join(f.responses, filepath.FromSlash(name))
	if f.mode == "cache" || (root && f.budget > 0) {
		if b, e := os.ReadFile(path); e == nil {
			if int64(len(b)) > max {
				return nil, fmt.Errorf("oversized cached response")
			}
			return b, nil
		}
	}
	if root && f.budget > 0 && f.fetched >= f.budget {
		return nil, fmt.Errorf("root-update-pending")
	}
	if !root && f.budget > 0 && !f.complete {
		return nil, fmt.Errorf("root-update-pending: no end-of-chain")
	}
	if f.mode == "cache" {
		if root {
			f.complete = true
			return nil, &metadata.ErrDownloadHTTP{StatusCode: 404, URL: url}
		}
		return nil, fmt.Errorf("cache miss: %s", name)
	}
	b, e := os.ReadFile(filepath.Join(f.origin, filepath.FromSlash(name)))
	if os.IsNotExist(e) {
		if root {
			f.complete = true
		}
		return nil, &metadata.ErrDownloadHTTP{StatusCode: 404, URL: url}
	}
	if e != nil {
		return nil, e
	}
	if int64(len(b)) > max {
		return nil, fmt.Errorf("oversized response")
	}
	if root {
		f.fetched++
	}
	// Untrusted bytes: replayed through the library from the ORIGINAL root on every run.
	if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return nil, e
	}
	if e = os.WriteFile(path, b, 0600); e != nil {
		return nil, e
	}
	return b, nil
}

func probe() (map[string]any, error) {
	if len(os.Args) != 7 {
		return nil, fmt.Errorf("usage: probe ROOT ORIGIN STATE live|cache ROOT-LIMIT ROOT-BUDGET")
	}
	if os.Args[4] == "archive" {
		return archive(os.Args[1], os.Args[2])
	}
	root, e := os.ReadFile(os.Args[1])
	if e != nil {
		return nil, e
	}
	limit, e := strconv.ParseInt(os.Args[5], 10, 64)
	if e != nil {
		return nil, e
	}
	budget, e := strconv.Atoi(os.Args[6])
	if e != nil {
		return nil, e
	}
	if os.Args[4] != "live" && os.Args[4] != "cache" {
		return nil, fmt.Errorf("invalid mode")
	}
	f := &probeFetcher{origin: os.Args[2], responses: filepath.Join(os.Args[3], "responses"), mode: os.Args[4], budget: budget}
	cfg, e := config.New("https://fixture.invalid/metadata/", root)
	if e != nil {
		return nil, e
	}
	cfg.RemoteTargetsURL = "https://fixture.invalid/targets/"
	cfg.LocalMetadataDir = filepath.Join(os.Args[3], "metadata")
	cfg.LocalTargetsDir = filepath.Join(os.Args[3], "targets")
	cfg.Fetcher = f
	if limit > 0 {
		cfg.MaxRootRotations = limit
	}
	u, e := updater.New(cfg)
	if e != nil {
		return nil, e
	}
	if e = u.Refresh(); e != nil {
		return nil, e
	}
	info, e := u.GetTargetInfo("catalog.json")
	if e != nil {
		return nil, e
	}
	if info.Length > 8*1024*1024 {
		return nil, fmt.Errorf("oversized catalog")
	}
	// Always read through the transport, including explicit cache replay mode.
	_, data, e := u.DownloadTarget(info, "", "")
	if e != nil {
		return nil, e
	}
	var catalog any
	if e = json.Unmarshal(data, &catalog); e != nil {
		return nil, e
	}
	return map[string]any{"status": "ok", "catalog": catalog, "root_version": u.GetTrustedMetadataSet().Root.Signed.Version, "new_roots": f.fetched}, nil
}

func main() {
	result, e := probe()
	if e != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"status": "error", "error": e.Error()})
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
