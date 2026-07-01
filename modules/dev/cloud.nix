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
      pkgs.tenv
      pkgs.cidr
    ];
  };
}
