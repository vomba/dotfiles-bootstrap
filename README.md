# dotfiles-bootstrap

Public flake base for Home Manager and nix-darwin dotfiles. It contains reusable modules, overlays, and example outputs that build without private secrets, hostnames, or local files.

The intended consumer is a private activation repo that imports this flake and layers personal identity, secrets, AI tooling, work settings, and machine-specific modules on top.

## Exports

- `homeManagerModules.bootstrap`
- `darwinModules.bootstrap`
- `overlays.default`
- `lib.mkPkgs`
- `lib.mkHomeConfiguration`
- `lib.mkDarwinConfiguration`
- `packages.<system>.zsh-kubecolor`
- `packages.<system>.mise` — hash-pinned official mise release binary

## Consumption

Private repos can use a GitHub flake URL directly:

```nix
{
  inputs.bootstrap.url = "github:<owner>/dotfiles-bootstrap";
}
```

Typical composition order:

1. import `inputs.bootstrap.homeManagerModules.bootstrap` or `inputs.bootstrap.darwinModules.bootstrap`
2. add private options and host-specific modules
3. layer private overlays after `inputs.bootstrap.overlays.default` if needed

## Portable profiles

Zsh includes a store-backed kubecolor plugin and its rsync helper. Kubernetes completion is generated at build time from the selected `kubectl`; startup never writes into the plugin directory. Oh My Zsh owns the single fzf Zsh integration. No private checkout path is required.

The Nord profile is disabled by default:

```nix
dotfiles.theme.nord.enable = true;
```

It styles enabled shell, Git, Helix, Yazi, and Glow consumers. Theme leaves are defaults, so ordinary consumer settings override them without `mkForce`. Existing unmanaged files retain Home Manager collision protection; the profile never forces replacement of `btop.conf`. Delta selects its custom syntax theme only when Bat's theme cache is enabled.

Portable DMS and kubecolor palettes are available under `themes/nord/`; the base does not deploy a desktop, choose a location/wallpaper, write a kubeconfig-adjacent file, or set private kubie exclusions. Source attribution and licenses accompany the assets.

Remove any old `dotfiles.desktop.hyprland.enable` assignment: the unused option has been removed. The base has no Hyprland module.

## Explicit mise tool installation

`dotfiles.dev.mise.enable` defaults to true, independently of the general
development flag. Cloud tooling requests CIDR 2.3.0 and Terraform 1.5.7 (the
last MPL-2.0 release); Kubernetes tooling requests Helmfile 1.8.1.
`apps/mise/mise.toml` owns these requests and backend aliases, and its committed
lock contains checksummed downloads for four Linux/macOS architectures.

Home Manager owns immutable `mise/config.toml` and its sibling `mise.lock`,
without forced replacement. After a separately authorized activation, run
`dotfiles-mise-install` explicitly to install selected tools and rebuild shims.
Activation never downloads tools. The installer ignores caller project,
environment and user/system mise configuration, but does not re-attest existing
tool/plugin storage. It preserves ordinary foreign shim collisions and fails
rather than reporting success; shim ownership follows mise's native heuristics,
and failures do not roll back entries already refreshed.

Bare commands use the shared `$XDG_DATA_HOME/mise-shims` directory. No mise shell
activation or prompt/chpwd hooks are enabled. Managed direnv/nix-direnv is
retired: use `mise exec -- command` for trusted project environment variables
and dotenv settings, and `nix develop` for Nix devshells. There is no automatic
Nix bridge. Explicit Nix environment commands can take precedence inside that
shell. Automatic installation and shim system-command fallback are disabled,
and normal resolution is offline; this is not a network sandbox for the tools
or trusted project code. Explicit installation goes online. Project Terraform
pins can override the global fallback; missing versions fail until installed.

Full `mise activate` is intentionally not used: in .17 it removes tool shims
when auto-install is disabled, allowing ordinary shell PATH lookup to bypass a
missing selected tool version. Explicit execution preserves the strict policy.

Personal additions can use unmanaged `config.local.toml` / `mise.local.lock`
with disjoint keys. `mise use --global` cannot edit HM-owned configuration.
Foreign configuration files retain Home Manager's normal collision protection.

The mise package uses official release archives with measured per-platform
hashes, source-matched completions, and self-update disabled. Native package
checks exercise the shipped executable; packaging does not compile or run the
upstream Rust test suite. Its exported package can be maintained with
`nix-update --flake mise`; review every changed release and source hash.

## Package updates

Helm plugin, OpenStack, and retained Nordic definitions live in `overlays/`. Check releases without changing files:

```bash
scripts/check-updates.py --dry-run
```

Explicitly apply complete supported OpenStack or Helm-secrets artifact updates:

```bash
scripts/check-updates.py --apply --yes
```

The checker owns only OpenStack and Helm-secrets release groups; Nordic variants
remain individually pinned and report without mutation. Unknown source shapes
fail closed. Exit status is `0` for current/no updates, `1` for available
updates, and `2` for an error. Explicit release checks query public GitHub
metadata; fixture tests do not contact providers.

Helm's base, SOPS, yq, and ordinary GNU Parallel come directly from nixpkgs.
The unused Parallel SQL-driver override is retired. CIDR and Helmfile are
mise-owned, while tenv retains every command and completion except its
`terraform` proxy; the filtered profile does not change tenv's source package
or downloaded user versions. Disabling mise does not restore retired Nix owners.
OpenStack remains 0.13.8 with its existing two-artifact/four-platform recipe.

The standalone nixpkgs, Home Manager and nix-darwin locked records are aligned
with the consuming configuration. The current Nordic overlay is retained
because the upstream package's SDDM/KDE dependency closure cannot be evaluated
on Darwin; no unsupported-platform override or partial theme migration is used.


## Layout

```text
flake.nix
flake.lock
AGENTS.md
lib/
modules/
overlays/
examples/
themes/nord/
scripts/check-updates.py
tests/
.github/workflows/ci.yml
```

## Verification

```bash
nix run nixpkgs#nixfmt -- --check flake.nix $(find modules -name '*.nix') $(find examples -name '*.nix') overlays/*.nix tests/*.nix
nix flake check
nix build .#example-linux --no-link
```

For `example-darwin`, use a macOS host or CI runner:

```bash
nix build .#example-darwin --no-link
```

On Linux, `nix flake check` will only execute checks for compatible systems unless you provide additional builders.

The example configurations intentionally disable the modules that usually need extra host setup, such as desktop GUI, cloud, and Kubernetes tooling.

Native checks include the example build, offline updater/plugin suites, optional Nord enable/disable/override boundaries, compiled kubecolor, and `mise-integration` (evaluated profile selections, immutable config/lock collisions, isolated installer, local-tool shim resolution, explicit project environments, and missing-version failures). Local immutable fixture tools do not attest downloaded release bytes; real artifact installation is a separate disposable smoke. The script check uses a writable disposable source tree and patched shebangs inside the Nix sandbox.

## CI

`.github/workflows/ci.yml`:

1. runs `nixfmt`
2. runs `nix flake check`
3. runs `gitleaks`
4. runs `shellcheck` on the retained kubectl-rsync helper
5. builds `example-linux` on Ubuntu and `example-darwin` on macOS

## Privacy Guardrails

This repo must not contain:

- real usernames or hostnames
- vault paths or secret filenames
- private cache names or tokens
- work-only domains or package registries
- tracked AI session artifacts

Before publishing changes, run a targeted leak scan using the private repo's sensitive identifiers as patterns. Also remember that any new `.nix` file must be tracked with `git add` before `nix flake check` will succeed.
