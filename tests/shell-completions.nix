{
  pkgs,
  home-manager,
  bootstrapModule,
}:
let
  inherit (pkgs) lib;
  mkHome =
    module:
    (home-manager.lib.homeManagerConfiguration {
      inherit pkgs;
      modules = [
        bootstrapModule
        {
          home.username = "bootstrap";
          home.homeDirectory =
            if pkgs.stdenv.hostPlatform.isDarwin then "/Users/bootstrap" else "/home/bootstrap";
          home.stateVersion = "26.05";
          home.fileActivator = "legacy";
          dotfiles.desktop.enable = false;
          dotfiles.apps.firefox.enable = false;
          dotfiles.shell.gpg.enable = false;
        }
        module
      ];
    }).config;
  profiles = {
    default = mkHome { };
    apps-disabled = mkHome { dotfiles.apps.enable = false; };
    cloud-disabled = mkHome { dotfiles.dev.cloud.enable = false; };
    kubernetes-disabled = mkHome { dotfiles.dev.kubernetes.enable = false; };
    mise-disabled = mkHome { dotfiles.dev.mise.enable = false; };
    zsh-disabled = mkHome { dotfiles.shell.zsh.enable = false; };
    yazi-disabled = mkHome { dotfiles.apps.yazi.enable = false; };
    renamed-yazi = mkHome { programs.yazi.shellWrapperName = "browse"; };
    git-disabled = mkHome { dotfiles.shell.git.enable = false; };
    renamed-lazygit = mkHome {
      programs.lazygit.enable = true;
      programs.lazygit.shellWrapperName = "gitui";
    };
    lazygit-wrapper-disabled = mkHome {
      programs.lazygit.enable = true;
      programs.lazygit.enableZshIntegration = false;
    };
  };
  completionPackage =
    config: lib.findFirst (p: lib.getName p == "dotfiles-zsh-completions") null config.home.packages;
  enabled = profiles.default;
  native = completionPackage enabled;
  assets = pkgs.buildEnv {
    name = "shell-completion-fixture-assets";
    paths = [
      native
      (lib.lowPrio pkgs.zsh-completions)
      pkgs.kubectl
      pkgs.openstackclient-full
      pkgs.ansible
      enabled.programs.mise.package
      enabled.programs.yazi.package
    ];
    pathsToLink = [ "/share/zsh" ];
  };
  executables = native.executables // {
    crossplane = lib.getExe pkgs.crossplane-cli;
    kubectl = lib.getExe pkgs.kubectl;
    openstack = "${pkgs.openstackclient-full}/bin/openstack";
    ansible = "${pkgs.ansible}/bin/ansible";
    mise = lib.getExe enabled.programs.mise.package;
    usage = lib.getExe pkgs.usage;
    yazi = lib.getExe enabled.programs.yazi.package;
  };
  binaries = pkgs.runCommand "shell-completion-fixture-bin" { } ''
    mkdir -p "$out/bin"
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: executable: ''
        ln -s ${lib.escapeShellArg executable} "$out/bin/${name}"
      '') executables
    )}
  '';
  profileData = lib.mapAttrs (
    _: config:
    let
      package = completionPackage config;
    in
    {
      package = if package == null then null else toString package;
      executables = if package == null then [ ] else builtins.attrNames package.executables;
      startup = pkgs.writeText "shell-completion-startup.zsh" config.programs.zsh.initContent;
      yaziWrapper = config.programs.yazi.shellWrapperName;
      yaziEnabled = config.programs.yazi.enable && config.programs.yazi.enableZshIntegration;
      lazygitWrapper = config.programs.lazygit.shellWrapperName;
      lazygitEnabled = config.programs.lazygit.enable && config.programs.lazygit.enableZshIntegration;
    }
  ) profiles;
in
assert enabled.programs.zsh.enableCompletion;
assert native.meta.priority == 9;
assert
  enabled.programs.zsh.oh-my-zsh.plugins == profiles.mise-disabled.programs.zsh.oh-my-zsh.plugins;
assert !enabled.programs.mise.enableZshIntegration;
assert !enabled.programs.mise.globalConfig.settings.auto_install;
assert !enabled.programs.mise.globalConfig.settings.not_found_auto_install;
assert !enabled.programs.mise.globalConfig.settings.task.run_auto_install;
assert completionPackage profiles.zsh-disabled == null;
pkgs.runCommand "shell-completions"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.coreutils
    ];
    COMPLETION_PROFILES = pkgs.writeText "shell-completion-profiles.json" (builtins.toJSON profileData);
    COMPLETION_ZSH = "${pkgs.zsh}/bin/zsh";
    COMPLETION_ASSETS = assets;
    COMPLETION_BIN = "${binaries}/bin";
    COMPLETION_OMZ = "${enabled.programs.zsh.oh-my-zsh.package}/share/oh-my-zsh";
    COMPLETION_HOME = enabled.home.homeDirectory;
    COMPLETION_MISE_CONFIG = enabled.xdg.configFile."mise/config.toml".source;
  }
  ''
    ${pkgs.python3}/bin/python3 ${./shell-completions.py}
    touch "$out"
  ''
