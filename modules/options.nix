{ lib, ... }:
{
  options.dotfiles = {
    desktop = {
      enable = lib.mkEnableOption "desktop environment" // {
        default = true;
      };
      hyprland = {
        enable = lib.mkEnableOption "Hyprland window manager" // {
          default = false;
        };
      };
      niri = {
        enable = lib.mkEnableOption "niri window manager" // {
          default = true;
        };
      };
    };
    dev = {
      enable = lib.mkEnableOption "development tools" // {
        default = true;
      };
      kubernetes = {
        enable = lib.mkEnableOption "Kubernetes tooling" // {
          default = true;
        };
        kubieConfigPath = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = "Optional path to a kubie config file to install at ~/.kube/kubie.yaml.";
        };
      };
      cloud = {
        enable = lib.mkEnableOption "cloud CLI tools" // {
          default = true;
        };
      };
      lsp = {
        enable = lib.mkEnableOption "language servers" // {
          default = true;
        };
      };
    };
    shell = {
      enable = lib.mkEnableOption "shell environment" // {
        default = true;
      };
      zsh = {
        enable = lib.mkEnableOption "Zsh configuration" // {
          default = true;
        };
      };
      git = {
        enable = lib.mkEnableOption "Git configuration" // {
          default = true;
        };
      };
      gpg = {
        enable = lib.mkEnableOption "GPG agent configuration" // {
          default = true;
        };
      };
    };
    apps = {
      enable = lib.mkEnableOption "misc packages" // {
        default = true;
      };
      firefox = {
        enable = lib.mkEnableOption "Firefox browser" // {
          default = true;
        };
      };
      editors = {
        enable = lib.mkEnableOption "code editors" // {
          default = true;
        };
      };
      cursor = {
        enable = lib.mkEnableOption "Cursor AI editor (IDE + CLI)" // {
          default = true;
        };
      };
      yazi = {
        enable = lib.mkEnableOption "Yazi file manager" // {
          default = true;
        };
      };
    };
  };
}
