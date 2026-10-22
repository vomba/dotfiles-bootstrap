{
  pkgs,
  config,
  lib,
  ...
}:
let
  manifest = builtins.fromTOML (builtins.readFile ../../apps/mise/mise.toml);
  selectedNames =
    lib.optionals config.dotfiles.dev.cloud.enable [
      "cidr"
      "terraform"
    ]
    ++ lib.optional config.dotfiles.dev.kubernetes.enable "helmfile";
  selectedTools = lib.filterAttrs (name: _: builtins.elem name selectedNames) manifest.tools;
  selectedAliases = lib.filterAttrs (
    name: _: builtins.hasAttr name selectedTools
  ) manifest.tool_alias;
  selectedConfig = {
    tools = selectedTools;
    tool_alias = selectedAliases;
    settings = {
      auto_install = false;
      not_found_auto_install = false;
      not_found_system_fallback = false;
      registry_floating = false;
      locked = true;
      locked_scopes = [ "global" ];
      idiomatic_version_file_enable_tools = lib.optional config.dotfiles.dev.cloud.enable "terraform";
      task.run_auto_install = false;
    };
  };
  tomlFormat = pkgs.formats.toml { };
  # Keep installer inputs separate from downstream task, environment and hook additions.
  installer = pkgs.callPackage ../../packages/mise-install.nix {
    mise = config.programs.mise.package;
    configFile = tomlFormat.generate "mise-install-config.toml" selectedConfig;
    lockFile = ../../apps/mise/mise.lock;
    commands = builtins.attrNames selectedTools;
  };
in
{
  config = lib.mkIf config.dotfiles.dev.mise.enable {
    programs.mise = {
      enable = true;
      package = pkgs.mise;
      mutableSettings = false;
      enableBashIntegration = false;
      enableZshIntegration = false;
      enableFishIntegration = false;
      enableNushellIntegration = false;
      globalConfig = selectedConfig;
    };

    xdg.configFile."mise/mise.lock".source = ../../apps/mise/mise.lock;

    home.packages = [ installer ];
    home.sessionPath = lib.mkBefore [ "${config.xdg.dataHome}/mise-shims" ];
    home.sessionVariables = {
      MISE_SHIMS_DIR = "${config.xdg.dataHome}/mise-shims";
      MISE_AUTO_INSTALL = "0";
      MISE_NOT_FOUND_AUTO_INSTALL = "0";
      MISE_NOT_FOUND_SYSTEM_FALLBACK = "0";
      MISE_OFFLINE = "1";
    };
  };
}
