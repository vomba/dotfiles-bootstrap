{
  pkgs,
  home-manager,
  bootstrapModule,
}:
let
  inherit (pkgs) lib;
  manifest = builtins.fromTOML (builtins.readFile ../apps/mise/mise.toml);
  lock = builtins.fromTOML (builtins.readFile ../apps/mise/mise.lock);
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
        }
        module
      ];
    }).config;
  profiles = {
    default = mkHome { };
    cloud-disabled = mkHome { dotfiles.dev.cloud.enable = false; };
    kubernetes-disabled = mkHome { dotfiles.dev.kubernetes.enable = false; };
    dev-disabled = mkHome { dotfiles.dev.enable = false; };
    mise-disabled = mkHome { dotfiles.dev.mise.enable = false; };
  };
  enabled = profiles.default;
  names = config: builtins.attrNames config.programs.mise.globalConfig.tools;
  installer = lib.findFirst (
    p: lib.getName p == "dotfiles-mise-install"
  ) (throw "mise installer missing from evaluated home.packages") enabled.home.packages;
  tenv = lib.findFirst (
    p: lib.hasSuffix "-without-terraform" p.name
  ) (throw "filtered tenv missing from evaluated home.packages") enabled.home.packages;
  profileInputs = lib.mapAttrs (_: config: {
    enabled = config.programs.mise.enable;
    config =
      if config.programs.mise.enable then
        toString config.xdg.configFile."mise/config.toml".source
      else
        null;
    tools = if config.programs.mise.enable then names config else [ ];
  }) profiles;
  # Absolute external version links are mise's supported local-link layout.
  # Aqua skips remote registry layout lookup for these links. The sidecar lives
  # at installs/<tool>/.mise.backend.toml, not inside the version directory.
  fixture =
    name: version:
    pkgs.writeShellScriptBin name ''
      printf '%s\n' 'fixture:${name}:${version}'
      printf '<%s>\n' "$@"
    '';
  fixtures = lib.mapAttrs (name: version: toString (fixture name version)) manifest.tools;
  secondTerraform = fixture "terraform" "1.4.6";
  collisionCheck = pkgs.writeShellScript "mise-check-home-collisions" enabled.home.activation.checkLinkTargets.data;
in
assert
  names profiles.default == [
    "cidr"
    "helmfile"
    "terraform"
  ];
assert names profiles.cloud-disabled == [ "helmfile" ];
assert
  names profiles.kubernetes-disabled == [
    "cidr"
    "terraform"
  ];
assert names profiles.dev-disabled == names profiles.default;
assert enabled.programs.mise.globalConfig.tools == manifest.tools;
assert lib.all
  (
    config:
    config.programs.mise.globalConfig.tool_alias
    == lib.filterAttrs (name: _: builtins.elem name (names config)) manifest.tool_alias
  )
  [
    profiles.default
    profiles.cloud-disabled
    profiles.kubernetes-disabled
    profiles.dev-disabled
  ];
assert !profiles.mise-disabled.programs.mise.enable;
assert !(profiles.mise-disabled.xdg.configFile ? "mise/config.toml");
assert !(profiles.mise-disabled.xdg.configFile ? "mise/mise.lock");
assert !(profiles.mise-disabled.home.sessionVariables ? MISE_SHIMS_DIR);
assert
  !(builtins.elem "${profiles.mise-disabled.xdg.dataHome}/mise-shims" profiles.mise-disabled.home.sessionPath);
assert
  !(lib.any (
    p:
    builtins.elem (lib.getName p) [
      "dotfiles-mise-install"
      "mise"
      "cidr"
      "helmfile"
    ]
  ) profiles.mise-disabled.home.packages);
assert !enabled.programs.mise.mutableSettings;
assert !enabled.programs.direnv.enable;
assert !enabled.programs.mise.enableBashIntegration;
assert !enabled.programs.mise.enableZshIntegration;
assert !enabled.programs.mise.enableFishIntegration;
assert !enabled.programs.mise.enableNushellIntegration;
assert !enabled.xdg.configFile."mise/config.toml".force;
assert !enabled.xdg.configFile."mise/mise.lock".force;
assert lib.all (
  name:
  let
    entries = lock.tools.${name};
  in
  builtins.length entries == 1
  && (builtins.head entries).version == manifest.tools.${name}
  && (builtins.head entries).backend == manifest.tool_alias.${name}
  &&
    builtins.filter (lib.hasPrefix "platforms.") (builtins.attrNames (builtins.head entries)) == [
      "platforms.linux-arm64"
      "platforms.linux-x64"
      "platforms.macos-arm64"
      "platforms.macos-x64"
    ]
) (builtins.attrNames manifest.tools);
pkgs.runCommand "mise-integration"
  {
    nativeBuildInputs = with pkgs; [
      python3
      bash
      coreutils
      findutils
      gettext
      git
    ];
    MISE_TEST_BIN = "${enabled.programs.mise.package}/bin/mise";
    MISE_TEST_INSTALLER = "${installer}/bin/dotfiles-mise-install";
    MISE_TEST_LOCK = enabled.xdg.configFile."mise/mise.lock".source;
    MISE_TEST_PROFILES = pkgs.writeText "mise-test-profiles.json" (builtins.toJSON profileInputs);
    MISE_TEST_MANIFEST = ../apps/mise/mise.toml;
    MISE_TEST_FIXTURES = pkgs.writeText "mise-test-fixtures.json" (builtins.toJSON fixtures);
    MISE_TEST_SECOND_TERRAFORM = secondTerraform;
    MISE_TEST_COLLISION_CHECK = collisionCheck;
    MISE_TEST_TENV = tenv;
    MISE_TEST_BASH = "${pkgs.bash}/bin/bash";
  }
  ''
    ${pkgs.python3}/bin/python3 ${./mise-integration.py}
    touch "$out"
  ''
