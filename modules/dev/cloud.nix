{
  pkgs,
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.dev.cloud.enable {
    home.packages = [
      pkgs.openstackclient-full
      pkgs.openstack-tui
      pkgs.upcloud-cli
      (pkgs.symlinkJoin {
        name = "${pkgs.tenv.name}-without-terraform";
        paths = [ pkgs.tenv ];
        postBuild = ''
          rm "$out/bin/terraform"
        '';
      })
    ];
  };
}
