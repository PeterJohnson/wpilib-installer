# Single-application manager: requirements and design evaluation

Date: November 27, 2026. Inspected prototype baseline: `037f1e7`.

Decision: proceed with **A2, Rust plus a private Qt engine**, as requested. The [single-application offline prototype](rust-manager-prototype.md) uses a Rust/egui first window and headless Qt worker, avoiding a second wizard. Native acceptance is tracked there; the full production gates below remain requirements. The earlier Go/Rust probes establish trust interoperability separately. Python remains the reference implementation and CI publication/test tooling; the Rust prototype now implements online TUF updates as well.

## Required user experience

The primary distribution requirement is **one user-facing application, preferably one executable; at most one GUI and one CLI**. Internal implementation language is secondary. These requirements apply to both initial installation and installed maintenance.

| ID | Acceptance requirement |
| --- | --- |
| UX-1 | Provide one Windows executable, one macOS application bundle, and one Linux executable/application artifact for each supported architecture. A macOS `.app` counts as one application even though it contains resources. An archive or disk image may transport it. Prefer one executable with GUI and CLI modes; the permitted fallback is one GUI plus one CLI, shipped together. |
| UX-2 | Double-click opens the manager GUI without a console flash, terminal commands, runtime installation, or a separate engine download. Show progress and actionable failures during bootstrap/verification; an invisible process that waits for network verification before showing its first window fails this requirement. |
| UX-3 | Users never choose between a WPILib launcher, Qt installer, and raw Qt maintenance tool. An embedded/extracted helper is permissible only as an implementation detail: no manual launch, separate acquisition, or separately exposed maintenance UI. Do not count a directory of independently operated programs as a single application. |
| UX-4 | The GUI selects online repositories or a browser-downloaded offline bundle and exposes profiles plus advanced package selection. A complete platform bundle remains a separate data download; this requirement does not put every package inside the manager executable. The manager must open/select the published bundle format without requiring a Python command. |
| UX-5 | Install a persistent manager entry point and discovery registration. After removing the USB drive and original download, the same product UI performs add/update/remove and can switch from offline installation to online updates. All ordinary shortcuts, OS uninstall entries, and VS Code launches enter the verification path. |
| UX-6 | Headless commands produce stable JSON, exit status, and cancellation behavior without opening a window or requiring a desktop display. Prefer the same application executable; use the permitted companion CLI if necessary for reliable Windows shell behavior. Both frontends use the same trust and planning implementation. |
| UX-7 | The application runs on a clean supported OS without separately installing Python, Go, Rust, Java, .NET, Qt, a VC redistributable, FUSE, or non-baseline GUI libraries. Declare minimum OS/desktop requirements and package required non-baseline libraries. “One file” does not by itself prove this. Test Linux executable-media and extraction restrictions explicitly. |
| UX-8 | Authenticate the distributed manager and its executable contents, use platform signing/notarization where required, and provide a recoverable self-update handoff. Replacement must preserve discovery paths and the verified maintenance entry point. Cancelled/failed updates leave a usable manager or an explicit repair path. |
| UX-9 | Preserve independent publisher trust, authenticated registry changes, verified caches, indefinitely authorized archival bundles, and sequential root catch-up beyond 256 rotations. No GUI, CLI, maintenance, or self-update route may silently bypass verification. |

The [self-contained runtime implementation](self-contained-runtime.md) defines the platform baselines, Linux host graphics with private Qt worker libraries, static Windows CRT, dependency audits and clean-OS checks for UX-7.

The single-application requirement concerns what the user downloads and operates, not a prohibition on internal processes, libraries, Qt inventory files, or installed packages. Hidden extraction must still preserve UX-2/5/7/8; it is not a blanket exemption. A separate GUI/CLI is the fallback ceiling, not permission to expose a third installer engine.

## What must move from Python

Both designs need the same installed-client capabilities ported: registry validation/refresh and revision floors (`registry.py`, client parts of `registry_updates.py`); TUF adaptation/root-history replay (`tuf_repository.py`); publisher manifests, dependency selection, ownership and Qt-index checks (client parts of `repository.py` and `payload.py`); downloads/hash caches, path/URL restrictions, signatures and deterministic JSON (`common.py`); verified loopback delivery (`gateway.py`); bundle/archival verification (client parts of `bundle.py` and `archival.py`); inventory, update discovery, Qt invocation (`manager.py`); and client commands (`cli.py`). These are behavior boundaries, not instructions to translate entire mixed client/publisher files.

Retain Python for upstream packaging, repository generation/signing/renewal, Artifactory/R2 publication, offline assembly/browser pages, and acceptance fixtures. Keep Qt's component inventory, archive extraction and built-in installation operations. Discovery, shortcuts, manager self-update, migration, richer planning, Gradle/VSIX setup and process checks are largely new work in either design; do not count them as implemented merely by porting current modules.

The cross-language signature boundary requires particular care: existing non-TUF envelopes use sorted, compact, ASCII-escaped JSON from `common.canonical()`. A default Go/Rust serializer is not automatically byte-compatible. Specify accepted values and golden signature/digest vectors, including Unicode and integer handling. Let the selected TUF library handle TUF serialization; do not conflate the two formats.

## A: native manager with a private Qt engine

Ship a native launcher/manager containing the trusted bootstrap, a Go or Rust client, and packaged native Qt installer engine. Evaluate A1 (Go/go-tuf) and A2 (Rust/tough) with the same process layout and UX requirements. On Windows/Linux, executable resources may be embedded and materialized into a private versioned directory. On macOS, use one app bundle with correctly placed nested executable code. Both languages support compile-time resource embedding; this establishes a packaging primitive, not a finished GUI. [Go embed](https://pkg.go.dev/embed), [Rust include_bytes](https://doc.rust-lang.org/std/macro.include_bytes.html)

The manager validates the source and plan, exposes only verified repositories, owns the child process lifetime, and invokes Qt for package operations. On installation it persists its own entry point and the required engine; Qt's generated maintenance tool remains private. Manager and engine compatibility are versioned together. The original download is not a required runtime dependency.

The selected prototype has Rust/egui own the first window and all progress, with Qt running headlessly. For other variants, the main design work is **who owns the first window**. A plain Go or Rust CLI that verifies everything and only then launches Qt fails UX-2. The chosen prototype supplies source selection, asynchronous progress and errors in Rust, and invokes only headless Qt commands. Cancellation remains a production gate. A variant that retains Qt's wizard would also need a controlled GUI handoff. Avoid two competing wizards, lost focus, duplicate taskbar/Dock entries and mismatched branding. A Qt-first frontend controlling a Go helper is another process arrangement, discussed below; do not assume it follows automatically from embedding the engine.

Advantages: preserves the current process/gateway boundary, minimizes dependence on Qt internals, and permits independent native client tests. Go uses `go-tuf/v2`; Rust uses `tough`. Neither requires a Qt fork or a C++ FFI for this process design; Python can continue producing repository metadata. Online tough refresh and offline installation are integrated in the native application prototype; production integration and hardening remain. [go-tuf](https://github.com/theupdateframework/go-tuf)

Costs: graphical bootstrap/handoff, two internal executable lifecycles, private extraction and cleanup, authenticated IPC, coordinated cancellation/elevation, and keeping all maintenance shortcuts away from the raw engine. An ordinary temporary-directory extraction followed by execution is not sufficient: use exclusive creation, restrictive access, embedded digest checks, and no untrusted helper search path. Do not run general network/UI logic elevated merely because an installation operation requires it.

### A1 versus A2: backend language within the same architecture

| Concern | A1: Go plus private Qt | A2: Rust plus private Qt |
| --- | --- | --- |
| Single download, private engine, GUI handoff | Same architecture and unresolved GUI work | Same; Rust does not imply an integrated Qt application |
| Repository interoperability | go-tuf 2.5.0 passed the tested Python-generated catalogs | tough 0.24.0 passed the same catalogs; the reported key-ID issue did not affect these fixtures |
| Root catch-up/cache | Needs the explicit replay/budget adapter | Needs the same policy plus transport-error normalization described in the results |
| Archival authorization | Native probe matches tested Python signature/policy vectors | Native probe matches the same vectors; define numeric bounds for a production port |
| Runtime acquisition | Compiled executable; no user-installed Go | Compiled executable; no user-installed Rust |
| Build work | Go dependency build; native Qt packaging remains | Cargo/native crypto dependency build; native Qt packaging remains |
| Later integrated UI | Could expose a native bridge if needed | Can reuse the core through a C ABI if later selecting B; no ABI needed for A2 |
| Selection | Credible peer; slightly simpler tested TUF adapter | Credible peer; choose based on maintainability/team fit after full UX gates |

Do not infer startup, memory or shipped-size superiority from language alone. Both still need the full installed-client port and platform lifecycle integration. The tested Rust candidate needs no library fork.

## B: integrated Qt IFW application with a Rust verification core

Build a customized Qt IFW `installerbase` with a Rust client core linked through a small C ABI. Qt owns the first window and lifecycle; the core supplies verification, planning, downloads, archival policy, and JSON discovery. Keep asynchronous work off the UI thread, define ownership/error/cancellation rules across the ABI, and prevent Rust panics from crossing it. Rust can produce a `staticlib` for foreign-language linking. [Rust linkage](https://doc.rust-lang.org/reference/linkage.html)

Qt's `binarycreator -t` accepts a custom template, and a static Qt build can avoid separately distributing Qt libraries. However, Qt explicitly describes its IFW C++ API as internal with **no compatibility promise**. This design means maintaining a source integration and native Qt/Rust builds, not installing a supported verification plugin. The integration must gate metadata/script loading before any unverified repository content executes, in initial installer, maintenance and CLI modes. [Template option](https://doc.qt.io/qtinstallerframework/ifw-tools.html#using-binarycreator), [static builds](https://doc.qt.io/qtinstallerframework/ifw-building-from-sources.html), [C++ API status](https://doc.qt.io/qtinstallerframework/ifw-cpp-classes.html)

Advantages: one window owner, no preflight-to-installer GUI handoff, and a direct place to implement common GUI/CLI policy. A linked core avoids an extracted verifier process. This is the cleaner fit for UX-1/2, provided the maintenance image retains the integration.

Costs: rebuilding and tracking Qt internals, the C++/Rust boundary, synchronous/asynchronous integration, and a larger platform build matrix. Rust `tough` remains a candidate rather than a production dependency: its key-ID interoperability issue is still open in the reviewed source. The native tests now verify our generated roots and existing catalogs successfully. That resolves the immediate fixture concern, not all metadata-extension/interoperability cases. [Tough issue 766](https://github.com/awslabs/tough/issues/766)

Do not replace Qt's engine with a custom installer merely to obtain this window layout. If source changes reach deep into package transactions or require a substantial fork, apply the architecture's existing decision rule and revisit the Avalonia fallback.

## Comparison against the actual product lifecycle

The assessments below are engineering judgments based on the inspected prototype and cited interfaces. They are not benchmark or acceptance results.

| Concern | A1/A2: Go or Rust launcher + private Qt | B: integrated Qt + Rust |
| --- | --- | --- |
| One application download | Feasible by embedded resources/app bundle; must include engine and bootstrap | Feasible with custom installerbase and linked core |
| First window, progress, errors | Rust/egui owns the implemented offline UI; Windows uses Direct3D, Linux/macOS OpenGL | Natural window ownership; still implement asynchronous verification UI |
| Package install/update/remove | Reuses existing Qt subprocess boundary | Reuses Qt engine but changes its startup/control integration |
| Persistent maintenance | Persist wrapper and route every registered launch through it; private Qt worker remains | Preserve custom code/bootstrap in the installed maintenance image and every engine update |
| Offline-to-online transition | Application policy port required | Same policy port required |
| GUI and headless CLI | Natural native CLI; Windows GUI launcher needs console/pipe testing or companion CLI | Shared core; headless dispatch must occur before GUI/display initialization |
| TUF/archival migration | Both passed the scoped native interoperability tests below; full manager policy/UX port remains | Same Rust core evidence can transfer; ABI/integration still untested |
| Signing | Sign outer application and runnable nested payloads; preserve bytes through extraction | Sign final custom installer and maintenance images; linked core reduces nested executable count |
| Self-update | Replace coordinated manager/engine version after old processes exit | Replace integrated image after it exits; still needs a private updater/handoff |
| Dependency on Qt internals | Lower, provided public CLI/scripts suffice | Higher; C++ API has no compatibility guarantee |
| Build/distribution work | Go or Cargo builds plus native Qt packaging and GUI bootstrap | C++/Rust/static-Qt builds and source-integration maintenance |
| Clean Linux machine | Current Qt binary needs additional libraries; packaging unresolved | Static Qt alone does not prove all system dependencies are included |
| Overall disposition | A2 (Rust) selected; offline application prototype implemented, production gates remain | Stronger integrated UX, higher maintenance cost; fallback if A cannot meet UX without comparable custom UI work |

**GUI ownership and backend language are separate decisions.** A Qt GUI with one Go CLI/helper could satisfy the user's allowed two-entry-point fallback while retaining `go-tuf`. It still needs a verified pre-fetch startup contract and installed-maintenance integration. Test whether public controller hooks suffice before assuming it avoids a source patch. Conversely, Go can be exposed through a native bridge; Rust is not technically required by an integrated UI. The process architecture and the language now have separate comparisons above.

## Platform and maintenance findings

Qt documents supplying a prebuilt signed maintenance component; on macOS it also uses a maintenance updater helper. Thus neither design can promise that initial executable signing automatically covers the installed or updated maintenance tool. Package the exact signed maintenance artifact and verify it after installation/update. The helper must remain internal. [Qt maintenance distribution](https://doc.qt.io/qtinstallerframework/ifw-updates.html#promoting-updates-for-the-maintenance-tool)

For macOS, sign nested code before the enclosing app, use the expected bundle locations, and keep writable cache/state outside signed resources. Replace the app through a controlled update rather than modifying sealed code/resources. Validate Developer ID distribution, notarization and offline assessment on actual downloaded artifacts; an unsigned CI build passing `codesign` on dashboard payloads is not evidence for manager distribution. [Apple signing guidance](https://developer.apple.com/library/archive/technotes/tn2206/_index.html)

For Windows, the PE subsystem distinguishes graphical and console applications. A GUI build must prove reliable redirected stdout/stderr, process waiting and exit codes for VS Code and shells. A console build that flashes a terminal at GUI startup fails acceptance. Use a companion CLI if the combined executable cannot meet both behaviors cleanly. [Windows subsystem](https://learn.microsoft.com/en-us/cpp/build/reference/subsystem-specify-subsystem)

For Linux, the inspected Qt 4.11 `installerbase` is a 44,984,824-byte dynamically linked ELF. `readelf -d` lists XCB/X11, xkbcommon, fontconfig, libc and other dependencies. This is an observed lower-level input size, not the proposed application's download size. Existing CI explicitly installs missing Qt dependencies. A packaged application/runtime or revised Qt build must satisfy a declared clean desktop baseline; an AppImage-style artifact would also need tests without assuming optional mount support. Both designs share this problem.

Select Qt build/linkage and redistribution terms explicitly before shipping a custom static build. The availability of source or a single-file output does not itself settle the distribution terms. [Qt overview and licensing](https://doc.qt.io/qtinstallerframework/ifw-overview.html)

## Decision and next acceptance experiment

Proceed with **A2: Rust plus a private Qt engine**. The user selected this direction after the trust comparison. Rust owns the application window and CLI; Qt remains a headless internal worker. Retain Python for CI packaging/signing/publication. The [online/offline application prototype](rust-manager-prototype.md) now integrates tough registry/publisher refresh, verified caches and native installation/maintenance. Go and integrated Qt/Rust remain evaluated alternatives, not parallel implementation tracks.

Require the following evidence on Windows x64, macOS x64/ARM64 and Linux x64. Windows/Linux ARM64 remain explicit feasibility gates; do not extrapolate the four-runner result to them.

| Gate | Required evidence | Current state |
| --- | --- | --- |
| G1: Launch and dependencies | Start the shipped artifact on clean supported desktops, with no developer runtimes/PATH; record first-window latency, cancellation/errors and visible windows/process entries | Native startup/screenshots and no-runtime-PATH checks implemented; clean OS/signing/cancellation remain |
| G2: Offline GUI install | Select the browser bundle with networking blocked; install real dashboards/profile; remove media and launch installed manager | Native application GUI-handler tests cover fixtures and both real dashboards; see application evidence |
| G3: Maintenance routing | Offline then online upgrades through desktop shortcut, OS maintenance entry, original download and VS Code; reject unauthenticated metadata before script execution | Installed manager supports offline maintenance after deleting the original media; online/OS/VS Code routing remain |
| G4: CLI | JSON/exit status/cancellation with redirected pipes and no display; GUI double-click has no console flash; shared policy with any companion CLI | Same executable JSON/exit-status/redirected-pipe checks implemented; cancellation and manual shell UX remain |
| G5: Trust equivalence | Python-produced catalogs and archival bundles, Unicode signature vectors, bad hashes/signatures, independent publishers, expired bootstraps, >256 and >1024 rotations, interrupted catch-up, poisoned cache and rollback | Online TUF probes passed on Linux x64 (results below); the offline application has separate native archival/cache checks. Online integration and cache migration remain |
| G6: Signed distribution | Download/quarantine the final artifact; verify initial, extracted/nested and installed maintenance code; assess macOS offline after notarization/stapling | Manager signing untested |
| G7: Self-update/recovery | Update manager/engine online and from media; inject failure before/after handoff, locked files and interrupted replacement; registered launch remains usable | Unimplemented |
| G8: Operational fit | Read-only USB/path with spaces/non-ASCII, insufficient space, extraction failure, proxy errors, concurrent operation and narrowly scoped elevation | A/B untested |

Record artifact hashes, toolchain/library versions, OS baseline, reports and GUI evidence for each gate. Measure compressed size, installed size, bootstrap peak disk and startup latency; do not choose from hypothetical size/performance estimates. A TUF smoke test or a successful `--help` invocation cannot close G1–G8.

## Evidence collected for this evaluation

- Read the current client, gateway, build configuration and status at `037f1e7`; no frozen manager or integrated native client exists there.
- Ran the pinned local `binarycreator --help`: confirms custom-template/resource options. This did not build or run a custom template.
- Inspected the local engine with `file`, `stat` and `readelf -d`: confirms the Linux dependency issue above.
- Reviewed IFW 4.11 source startup, which selects installer or maintenance GUI through the same application implementation. This identifies an integration area, not a demonstrated secure pre-fetch hook. [Pinned installerbase source](https://github.com/qtproject/installer-framework/blob/4.11.0/src/sdk/installerbase.cpp)
- Reviewed the primary references linked above. The library/probe results below are distinct from the later [native application tests](rust-manager-prototype.md). Signing/notarization and full production gates are not closed by either experiment.


## Native library interoperability results

On November 27, 2026, Linux x64 probes built with **Go 1.27.4 / go-tuf 2.5.0** and **Rust 1.100.0 / tough 0.24.0** passed **69 checks: 34 Go, 35 Rust**. Both libraries were used without patches. The extra Rust check records its unadapted transport-error behavior. [Saved results](evidence/native-tuf-2026-11-27.json), [sources and reproduction commands](../experiments/tuf-clients/README.md), [test driver](../tools/check_native_tuf.py).

| Tested behavior | Result for both clients |
| --- | --- |
| Existing real-dashboard repository catalogs | Registry, Elastic and AdvantageScope catalogs match the Python client's result; no repository mutation |
| Publication/update/rollback | Python publication APIs generate accepted metadata; later update accepted and old timestamp rejected |
| Verified cache with origin removed | Catalog succeeds through current-clock TUF verification; tampering with all timestamp/snapshot/targets copies or catalog bytes is rejected |
| Authentic expired cached metadata | Rejected without altering the native clients' clocks or disabling expiry |
| Independent publisher roots | Substitution rejected |
| 1,030 sequential roots | Expired bootstrap/intermediates accepted only along verified transitions to a current final root; old and new two-of-three thresholds checked at rotation 257 |
| Interrupted catch-up | Four 256-root calls return pending without a catalog; fifth replays the original chain and finishes with five additional roots; cache corruption on resume rejected |
| Broken root chain | Missing/skipped roots, insufficient signatures on either side, and expired final root rejected |
| Archival authority isolation | Rotating the archival key leaves TUF online/cache verification working; an archival signer alone cannot forge an accepted online catalog |
| Native archival authorization | Existing schema-3 indefinite envelopes accepted despite expired online authorization; wrong pins, signature tampering, registry binding/policy/provenance changes rejected |
| Cross-language signed JSON | Unicode keys, emoji, control characters, `<>&`, booleans/null and integers above 2^53 match Python-generated signatures/digests |
| Old media followed by online update | Native authorization accepts the old archival snapshot, then native TUF reaches root 1030 and a newer registry signed by a rotated archival key; old offline authorization remains usable |

Three findings affect implementation:

1. **Neither default root limit is sufficient.** Go's default is 256; tough's is 1,024. The probes first confirm failure on the long fixture, then pass using an explicit root-history replay/budget adapter. These checks are evidence for the adapter design, not built-in unlimited catch-up.
2. **Tough needs transport-error adaptation.** In 0.24.0 root loading, an initial `fetch` error is treated as end-of-chain. A batch interruption then appears as an expired-root failure in our fixture. Returning non-missing-file failures through the stream preserves pending/error semantics; a regression test retains both behaviors. [Versioned source](https://docs.rs/tough/0.24.0/src/tough/lib.rs.html)
3. **A cache test must account for every valid copy.** Go legitimately used its valid metadata store when a transport-cache snapshot/targets copy was corrupted. The final rejection checks corrupt all copies, rather than treating recovery through valid bytes as a vulnerability.

Scope limits are deliberate. Directory-backed transports supply the exact repository bytes through a synthetic URL namespace, so these results do not validate HTTPS/proxy behavior. The archival probes cover schema-3 indefinite authorization, not complete bundle payload extraction/verification or legacy bounded schemas. The existing Python archival implementation is also used as an oracle, including its year-2100 test; native authorization is separately exercised with already-expired online authorization and performs no TUF/cache access. Native clocks are never moved backward. The programs are experiments with caller-supplied trust inputs, not hardened production entry points.

These library probes alone establish no Windows/macOS execution, clean-machine GUI, signing, self-update or installed-manager routing. The subsequent application experiment is documented separately. Probe byte sizes are included for reproducibility but are not an apples-to-apples production size benchmark. These results supported choosing **Rust plus private Qt**; they do not imply G1–G8 are complete.
