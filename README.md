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

## Package updates

Generic Helm, Helmfile, OpenStack, and Nordic definitions live in `overlays/`. Check releases without changing files:

```bash
scripts/check-updates.py --dry-run
```

Explicitly apply complete supported OpenStack or Helm-secrets artifact updates:

```bash
scripts/check-updates.py --apply --yes
```

Go source/vendor updates (Helm, Helmfile, CIDR) remain manual. SOPS and yq use nixpkgs directly, without custom overrides. Nordic variants remain individually pinned. Unknown source shapes fail closed. Exit status is `0` for current/no updates, `1` for available updates, and `2` for an error. Checks/tests do not use credentials or make provider calls; explicitly running the release checker queries public GitHub release metadata.

The nixpkgs lock supplies Go 1.27 for Helmfile 1.8.1 (Go >= 1.27.1), SOPS 3.13.3 and yq 4.54.1. Its compatibility pin was updated without changing the Home Manager or nix-darwin inputs.

The retained standalone Home Manager pin still emits upstream deprecated
`stdenv.isLinux` / `stdenv.isDarwin` alias warnings with this nixpkgs version
(for example, its `modules/systemd.nix` default). Native checks and the example
generation build pass; this package refresh does not repin Home Manager.


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

Native checks include the example build, offline updater/plugin behavior suites, optional Nord enable/disable/override boundaries, and the compiled kubecolor package. The script check uses a writable disposable source tree and patched shebangs so fixtures work inside the Nix sandbox.

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
