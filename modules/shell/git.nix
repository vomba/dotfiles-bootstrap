{
  pkgs,
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.shell.git.enable {
    programs.git = {
      enable = true;
      settings = {
        url."ssh://git@github.com/".insteadOf = "https://github.com/";
        diff.sopsdiffer.textconv = "sops -d";
      };
    };

    home.packages = [
      pkgs.lazygit
      pkgs.git-crypt
    ];
  };
}
