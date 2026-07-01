# dotfiles-bootstrap

Public bootstrap repo for reusable Nix modules.

## Rules

- Keep private identity, secrets, work config, caches, vaults, and AI tooling out of this repo.
- `imports` belong at the top of each Nix module.
- Wrap module content with `lib.mkIf` on the matching `dotfiles.*.enable` option.
- If a module references a new `.nix` file, `git add` it before running `nix flake check`.
- Example outputs must build without local files such as `kubie.yaml`, `rbw-config.json`, or `credentials-helper.bash`.
- Prefer documenting this repo as a reusable flake base, not a copy-paste template.
- Use placeholders such as `github:<owner>/dotfiles-bootstrap` in docs instead of hardcoding real owner names.

## Verification

```bash
nix run nixpkgs#nixfmt -- --check flake.nix $(find modules -name '*.nix') $(find examples -name '*.nix') overlays/*.nix
nix flake check
nix build .#example-linux --no-link
```

For Darwin verification, use a macOS runner or host:

```bash
nix build .#example-darwin --no-link
```
