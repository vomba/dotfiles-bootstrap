{
  pkgs,
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.apps.enable {
    home.packages = [
      pkgs.golazo
      pkgs.gh
      pkgs.yq-go
      pkgs.jq
      pkgs.jless
      pkgs.glow
      pkgs.act
      pkgs.hugo
      pkgs.socat
      pkgs.sops
      pkgs.bitwarden-cli
      pkgs.rbw
      pkgs.yubikey-manager
      pkgs.nodejs_24
      pkgs.python3
    ]
    ++ lib.optionals pkgs.stdenv.isLinux [
      pkgs.powershell
    ];
  };
}
