# dotfiles-bootstrap

Public native source/assets for **Ubuntu 24.04 LTS x86_64** and **macOS arm64 (15+)**. The engine composes manifests, stages authored configuration with chezmoi, provisions selected tools through mise/native providers, and handles recorded ownership/recovery. Nix is not required to bootstrap, render, build recipes, verify or run the environment.

Private consumers keep identity, secrets, work, AI tooling and host configuration in their own repository. This public source has no private hook requirement or private user inputs when run standalone.

## Consumption and ownership

A consumer's shell entry reads its `bootstrap.lock.json`: HTTPS repository, exact reviewed Git commit and SHA-256 of its Git tar archive. It acquires and validates that source into an immutable managed cache, checks cached bytes before importing engine code and invokes the engine with the consumer root. There is no required separately maintained checkout, mutable-cache update or second chezmoi state.

The public checkout also has `./bootstrap` for standalone use. `--bootstrap-source PATH --verification-only --home DISPOSABLE_HOME` is an explicit local integration route, not a production override or host-mutation permission. An unpublished consumer pin fails closed.

[Linux](examples/linux.json) and [macOS](examples/macos.json) are complete identity-free manifest fragments, not preinstalled profiles or activation scripts. A consumer can place the selected fragment in its own `native/manifests/` (which composes JSON fragments), review its defaults alongside the other declarations, and use `plan` / `render --output EMPTY_STAGE` before any authorized mutation. They contain no private hook, identity, credentials or work/AI wiring.

| Owner | Responsibility |
|-------|----------------|
| `native/manifests/` | defaults, feature/platform selectors, package/service/artifact identities |
| `native/chezmoi/` | authored portable files/templates; collision-aware installation |
| `apps/mise/` | requests, multi-platform lock and locked npm Aube graphs |
| `native/engine/` | validated root operations, providers, immutable source, ownership journals/recovery |
| `native/recipes/` | exceptional desktop/tool/completion assemblies with explicit provenance |
| `native/gui/` | native GUI application delivery |
| `native/assets/`, `themes/nord/` | portable assets, notices and narrow mutable-config adapters |
| `native/verification/` | pinned internal native checker tooling and verification |
| consumer private hook | consumer-specific files/reconciliation only; no private code lives here |

Chezmoi is not the owner of application databases or mutable user settings. Mise is not a competing dotfiles writer. APT/Homebrew/recipes have explicit installation ownership; shared OS packages/services are retained rather than globally pruned. Reading source or running tests is not activation authority.

## Operations

Run `./bootstrap --help` for the exact current options. Representative standalone operations:

```sh
./bootstrap bootstrap --yes
./bootstrap native-host status
./bootstrap plan
./bootstrap render --output /absolute/path/to/empty-stage
./bootstrap updates
./bootstrap updates --refresh
./bootstrap services status
```

Bootstrap uses native shell/Git/curl/tar/SHA tools and checksum-pinned standalone Python, mise and chezmoi. Engine Python, interactive Python and scoped recipe interpreters are separate ownership scopes. Mutating operations require `--yes` **and explicit authorization**:

```sh
./bootstrap provision --yes
./bootstrap apply --yes
```

Provision installs selected packages. Apply only installs staged authored configuration and performs declared narrow reconciliation; it never implicitly downloads packages, decrypts credentials or starts services. Service transitions are separate `services OPERATION --id SERVICE_ID --yes` actions. IDs come from `plan`/manifests. Unknown flags/action combinations fail before mutation.

The shell entry checks CLI syntax, platform/operation combinations and isolated-home boundaries before acquiring Python. Help remains available without prerequisites. Feature override syntax is checked there; declared boolean-leaf existence is validated by the composed manifests in the engine.

Feature overrides use `--set FEATURE=true|false`. Public defaults compose with the selected `linux-x64` / `macos-arm64` profile, then consumer declarations. A render for the other platform is not native execution. Disabled features do not resurrect old owners; retirement requires exact unchanged recorded ownership and preserves foreign replacements/data.

### macOS native host prerequisites

`native-host status` inspects Apple CLT/default-prefix Homebrew readiness. `native-host clt-request --yes` explicitly invokes Apple's install request; the user completes Apple consent/installation and reruns status. `native-host provision --yes` runs the official revision/digest-pinned Homebrew installer in a scrubbed native environment as a non-root managing user. Normal Apple sudo authorization may prompt; no credentials are stored or license/TCC consent bypassed. Existing valid `/opt/homebrew` installations remain shared and retained; retirement never uninstalls Homebrew/formulas/data. On Linux native-host is not-required.

## Portable shell, tools and themes

Zsh owns one Oh My Zsh compinit. Completion fpath precedence is package-native → generated → private → supplemental; private integration runs afterward. Supported fallback assets are generated during explicit provisioning, not at shell startup. Dynamic upstream resource completers may still access services; they are not sandboxed. Generic plugins and kubectl-rsync remain public; private helpers do not.

The Nord profile is opt-in via `theme.nord.enable`; assets cover enabled shell, Git, Helix, Yazi and Glow without whole-file ownership of mutable Btop state. Firefox configuration preserves user profile data and signed-extension consent boundaries. Linux GUI recipes use native dependencies, not package-store GPU wrappers. Mac apps have native bundle/registration paths.

`dev.mise.enable` controls managed global config, shared strict shims and the explicit catalog, independently of general dev enablement. `apps/mise/mise.toml`, `mise.lock` and `.mise/locks/` are the canonical selected request/lock graph. `dotfiles-mise-install` isolates caller/ancestor config and installs explicitly; no shell activation, prompt/chpwd hooks, missing-version system fallback or automatic download. Personal config.local/lock additions must use disjoint keys; do not edit managed files with mise use --global. `mise exec -- command` is an explicit trusted-project environment action.

Interactive Python is apps-owned; OpenStack uses its separately locked Python 3.12 ABI environment. Terraform retains 1.5.7 for the declared license policy. CIDR/Terraform require cloud+mise; Helmfile Kubernetes+mise. Tenv keeps other tools but no competing Terraform proxy. Helm retains diff/secrets/getter/post-renderer plugins. The native tools recipe owns the full frozen OpenStack plugin assembly plus Rust CLI/TUI and declared native dependencies.

## Desktop recipes

`native/recipes/desktop/` declares exact stable Niri/satellite source/dependency locks, complete Noble .deb/session integration, official Dux 2.0, isolated Quickshell + private Qt/Wayland runtime, reviewed Noble-targeted Hyprland PPA and appearance provenance. Do not add a newer Ubuntu release repository, replace global Qt/graphics libraries, export global Qt/QML/LD_LIBRARY_PATH or treat temporary research binaries as production inputs. Private Qt ABI changes require rebuilding Quickshell. Qt 6.11 standard support ends 2027-03-17; qualify a supported successor before that date without assuming commercial LTS access.

The root exposes `desktop status|inspect|acquire|candidate|recover`; mutations require explicit --yes. Select `--id PACKAGE` (default `niri`). Acquisition defaults to a versioned consumer-managed source-update candidate lock, never the immutable public cache; explicit `--candidate-output` stays within the consumer repository or managed workspace. Desktop `--output BUILD_DIR` is valid only for `candidate`, paired with a complete `--candidate LOCK`. Toolchain version overrides are acquisition/source-preparation options, not status/recovery/build options. A prepared desktop source candidate also comes from the managed source workflow. Production recipes include runtime dependencies/session/portal/service/license payloads, not merely executables. This public source does not supply private monitors, wallpaper/location, identity, Dux user state or AI/work configuration.

A new compositor is selected at deliberate logout/login, never automatic restart of a working session. Nested/container UI tests do not prove physical GPU/DRM, GDM/PAM, portals/screenshare, audio/Bluetooth, dock/VRR/scaling, lock/polkit/keyring, suspend or recovery.

## Updates and dependency workflows

`updates [--refresh]` reports upstream/provider/selected/installed distinctions. A distro provider reporting current is not proof of latest upstream. Policy exceptions (distro-security, project ABI, license pins) are explicit with reasons.

Consumer-owned source workflow:

```sh
./bootstrap source status
./bootstrap source prepare --family all --yes
./bootstrap source check
# Only with separate signed-publication authority:
./bootstrap source publish --yes
```

`source prepare --family mise|desktop|all --yes` creates/updates a managed public-only Git worktree derived from the consumer's immutable pin and produces coherent mise/Aube or desktop source locks. Desktop acquisition overrides are `--qt-version`, `--rust-version`, `--wayland-version` and `--protocols-version`. It returns concrete candidate paths; it does not publish, mutate selected/live state or edit the immutable cache. `source check` validates both composed platforms. Desktop candidates are explicitly built/qualified before promotion.

`source publish` alone signs a public source commit, performs normal non-force push, fetches fresh source identity and updates the consumer's narrow private source pin. It never commits private state or publishes built binaries. `source recover --yes` resumes recorded interrupted publication/pin transitions, without force resets or foreign-edit destruction. Source publication requires separate approval; updating instructions is not that approval. Public source data in a pinned immutable cache cannot be promoted with ordinary update --promote; the source workflow replaces the pin instead. Private update bundles use their own explicit candidate/promotion contract.

Update versions, hashes, platform variants, runtime dependencies and lock graphs together. Keep known-good artifacts until a qualified candidate succeeds. Report upstream compatibility gaps rather than silently downgrading or claiming latest. Review licenses/notices before any separately authorized custom binary distribution.

Crossplane follows the independently released `crossplane/cli`, not the server
release version. Supported-platform downloads must have committed SHA-256 pins.
For older releases without publisher checksums, reviewed hashes of the actual
HTTPS artifacts are trust-on-first-use pins, not publisher attestations. Native
mise refresh retains matching pins; missing integrity on a new selection blocks
source checking rather than silently accepting an unverified download.

## Verification

Native check dependencies are pinned internal tools, not runtime downloads from caller project configuration:

```sh
./bootstrap bootstrap --verification-tools --yes
./bootstrap verify --checks
```

`verify --checks --files-from JSON` selects explicit paths; `--no-tests` excludes behavior tests and is reduced scope. The public CI workflow exercises native Linux/macOS public-only render/behavior/security/style checks, not private inputs. Consumer private suites remain local. Follow actual verifier help and report optional native-tool skips honestly; no Nix devshell/flake checks remain.

The static `provider-locks` gate reuses source-publication validation across both
supported targets and public/private compositions. It remains mandatory with
`--no-tests` or a selected-file list; an uninstalled tool is not an excuse to omit
its lock integrity. Rendered shell validation also remains active without behavior
tests.

Behavior tests use disposable HOME/synthetic credentials, actual rendered outputs, ownership/feature/security contracts and real supported native tools where available. GUI/login/provider authentication and another platform are not proven by a source render or unit suite. Workers do not run integration gates mid-flight; the integration owner verifies the complete union and applies code/security review after implementation.

## Lifecycle and privacy limits

Inspection, shell/login registration, ownership handoff, native rescue and Nix retirement are distinct `lifecycle` operations for consumers with declared lifecycle support. Handoff needs exact old ownership evidence; no inference from a store path or service name. Preserve foreign files, active sessions and shared Docker/Snap/OS resources. Native rescue must work through the OS shell and standalone interpreter without Nix. Installer-aware Nix retirement requires its own authorization and native runtime/login/services/rescue/host acceptance; unsupported/ambiguous installers fail closed. Never guess rm /nix or broad cleanup.

This repository must not contain identity, secrets/encrypted private inputs, credentials, work hosts/paths, OMP/Zed/ACP/MCP/skills, vault/memory/session data or private hostnames. Public examples use placeholders only. No source publication implies workstation activation, credential access, consent completion or built-artifact publication.
