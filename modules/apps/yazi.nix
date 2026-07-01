{
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.apps.yazi.enable {
    programs.yazi = {
      enable = true;
      enableZshIntegration = true;
    };
  };
}
