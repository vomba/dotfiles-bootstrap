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

## Layout

```text
flake.nix
flake.lock
AGENTS.md
lib/
modules/
overlays/
examples/
.github/workflows/ci.yml
```

## Verification

```bash
nix run nixpkgs#nixfmt -- --check flake.nix $(find modules -name '*.nix') $(find examples -name '*.nix') overlays/*.nix
nix flake check
nix build .#example-linux --no-link
```

For `example-darwin`, use a macOS host or CI runner:

```bash
nix build .#example-darwin --no-link
```

On Linux, `nix flake check` will only execute checks for compatible systems unless you provide additional builders.

The example configurations intentionally disable the modules that usually need extra host setup, such as desktop GUI, cloud, and Kubernetes tooling.

## CI

`.github/workflows/ci.yml`:

1. runs `nixfmt`
2. runs `nix flake check`
3. runs `gitleaks`
4. runs `shellcheck` only if `scripts/*.sh` exists
5. builds `example-linux` on Ubuntu and `example-darwin` on macOS

## Privacy Guardrails

This repo must not contain:

- real usernames or hostnames
- vault paths or secret filenames
- private cache names or tokens
- work-only domains or package registries
- tracked AI session artifacts

Before publishing changes, run a targeted leak scan using the private repo's sensitive identifiers as patterns. Also remember that any new `.nix` file must be tracked with `git add` before `nix flake check` will succeed.
