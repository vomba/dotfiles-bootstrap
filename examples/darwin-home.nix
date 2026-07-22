{
  home.username = "bootstrap";
  home.homeDirectory = "/Users/bootstrap";
  home.stateVersion = "26.05";

  programs.home-manager.enable = true;
  programs.yazi.shellWrapperName = "yy";

  dotfiles.desktop.enable = false;
  dotfiles.dev.cloud.enable = false;
  dotfiles.dev.kubernetes.enable = false;
  dotfiles.apps.firefox.enable = false;
  dotfiles.apps.cursor.enable = false;
}
