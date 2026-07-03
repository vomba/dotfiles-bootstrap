{ ... }:
{
  imports = [
    ./options.nix
    ./shell/shell.nix
    ./shell/zsh.nix
    ./shell/gpg.nix
    ./shell/git.nix
    ./apps/firefox.nix
    ./apps/editors.nix
    ./apps/packages.nix
    ./apps/yazi.nix
    ./dev/dev.nix
    ./dev/cloud.nix
    ./dev/kubernetes.nix
    ./dev/lsp.nix
  ];
}