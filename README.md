# dotfiles-bootstrap

Public flake base for Home Manager and nix-darwin dotfiles. It provides reusable modules, overlays, and conservative example outputs that build without private secrets, hostnames, or local files.

## Exports

- `homeManagerModules.bootstrap`
- `darwinModules.bootstrap`
- `overlays.default`
- `lib.mkPkgs`
- `lib.mkHomeConfiguration`
- `lib.mkDarwinConfiguration`

## Layout

```text
flake.nix
AGENTS.md
modules/
overlays/
examples/
.github/workflows/ci.yml
```

## Example Outputs

```bash
nix flake check
nix build .#example-linux
nix build .#example-darwin
```

The examples intentionally disable the modules that usually need extra host setup, such as desktop GUI, cloud, and Kubernetes tooling.

## Privacy Guardrails

This repo must not contain:

- real usernames or hostnames
- vault paths or secret filenames
- private cache names or tokens
- work-only domains or package registries
- tracked AI session artifacts

Run a quick leak scan before publishing, using patterns from your private repo for usernames, hostnames, work domains, secret names, and vault paths.
