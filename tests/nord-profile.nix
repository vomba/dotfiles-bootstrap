{
  pkgs,
  home-manager,
  bootstrapModule,
}:
let
  mkHome =
    module:
    (home-manager.lib.homeManagerConfiguration {
      inherit pkgs;
      modules = [
        bootstrapModule
        {
          home.username = "bootstrap";
          home.homeDirectory = "/home/bootstrap";
          home.stateVersion = "26.05";
          dotfiles.desktop.enable = false;
          dotfiles.dev.cloud.enable = false;
          dotfiles.dev.kubernetes.enable = false;
          dotfiles.apps.firefox.enable = false;
        }
        module
      ];
    }).config;
  disabled = mkHome { dotfiles.theme.nord.enable = false; };
  enabled = mkHome { dotfiles.theme.nord.enable = true; };
  overridden = mkHome {
    dotfiles.theme.nord.enable = true;
    programs.starship.settings.username.style_user = "consumer-style";
    programs.fzf.colors.prompt = "consumer-prompt";
    programs.delta.options.syntax-theme = "consumer-syntax";
    programs.helix.settings.theme = "consumer-editor";
    programs.yazi.theme.flavor.dark = "consumer-flavor";
    home.sessionVariables.BAT_THEME = "consumer-bat";
  };
  shellDisabled = mkHome {
    dotfiles.theme.nord.enable = true;
    dotfiles.shell.enable = false;
  };
  batDisabled = mkHome {
    dotfiles.theme.nord.enable = true;
    programs.bat.enable = pkgs.lib.mkForce false;
  };
  appDisabled = mkHome {
    dotfiles.theme.nord.enable = true;
    dotfiles.apps.editors.enable = false;
    dotfiles.apps.yazi.enable = false;
    dotfiles.apps.enable = false;
  };
in
assert !(disabled.home.sessionVariables ? BAT_THEME);
assert !(disabled.xdg.configFile ? "btop/themes/nord-contrast.theme");
assert !(disabled.programs.helix.themes ? nord-contrast);
assert enabled.programs.bat.config.theme == "nord-contrast";
assert enabled.programs.btop.settings.color_theme == "nord-contrast";
assert enabled.programs.helix.settings.theme == "nord-contrast";
assert enabled.programs.yazi.theme.flavor.dark == "nord";
assert overridden.programs.starship.settings.username.style_user == "consumer-style";
assert
  overridden.programs.starship.settings.git_branch == enabled.programs.starship.settings.git_branch;
assert overridden.programs.fzf.colors.prompt == "consumer-prompt";
assert overridden.programs.fzf.colors.fg == enabled.programs.fzf.colors.fg;
assert overridden.programs.delta.options.syntax-theme == "consumer-syntax";
assert overridden.programs.helix.settings.theme == "consumer-editor";
assert overridden.programs.yazi.theme.flavor.dark == "consumer-flavor";
assert overridden.programs.yazi.theme.flavor.light == enabled.programs.yazi.theme.flavor.light;
assert overridden.home.sessionVariables.BAT_THEME == "consumer-bat";
assert !(shellDisabled.home.sessionVariables ? BAT_THEME);
assert !(shellDisabled.xdg.configFile ? "btop/themes/nord-contrast.theme");
assert !(shellDisabled.programs.delta.options ? syntax-theme);
assert !(batDisabled.programs.delta.options ? syntax-theme);
assert !(batDisabled.home.sessionVariables ? BAT_THEME);
assert !(appDisabled.programs.helix.themes ? nord-contrast);
assert !(appDisabled.programs.yazi.flavors ? nord);
assert !(appDisabled.xdg.configFile ? "glow/glow.yml");
pkgs.runCommand "nord-profile" { } ''
  touch "$out"
''
