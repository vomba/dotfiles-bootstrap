{
  pkgs,
  config,
  lib,
  ...
}:
let
  isDarwin = pkgs.stdenv.hostPlatform.isDarwin;
  isLinux = pkgs.stdenv.hostPlatform.isLinux;
in
{
  config = lib.mkIf config.dotfiles.shell.gpg.enable {
    programs.gpg = {
      enable = true;
      settings.trust-model = "tofu+pgp";
    };

    services.gpg-agent = {
      enable = true;
      enableSshSupport = true;
      enableZshIntegration = true;
      pinentry.package =
        if isDarwin then
          pkgs.pinentry_mac
        else if isLinux then
          pkgs.pinentry-qt
        else
          null;
    };

    home.packages = lib.optional isDarwin pkgs.pinentry_mac ++ lib.optional isLinux pkgs.pinentry-qt;
  };
}
