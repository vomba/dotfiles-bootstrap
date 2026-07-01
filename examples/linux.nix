{ pkgs, lib, ... }:
{
  home.username = "bootstrap";
  home.homeDirectory = "/home/bootstrap";
  home.stateVersion = "26.05";

  programs.home-manager.enable = true;
  fonts.fontconfig.enable = true;
  programs.firefox.configPath = lib.mkIf pkgs.stdenv.isLinux ".mozilla/firefox";
  programs.yazi.shellWrapperName = "yy";

  nix.package = lib.mkIf pkgs.stdenv.isLinux pkgs.nix;
  nix.settings = {
    max-jobs = 4;
    min-free = "2G";
    max-free = "10G";
    auto-optimise-store = true;
    substituters = [
      "https://nix-community.cachix.org"
      "https://cache.nixos.org"
    ];
    trusted-public-keys = [
      "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
    ];
  };

  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };

  dotfiles.desktop.enable = false;
  dotfiles.dev.cloud.enable = false;
  dotfiles.dev.kubernetes.enable = false;
  dotfiles.apps.firefox.enable = false;
  dotfiles.apps.zed.enable = false;
}
