{
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.shell.zsh.enable {
    home.sessionVariables = {
      GOPATH = "${config.home.homeDirectory}/.go";
      KREW_ROOT = "${config.home.homeDirectory}/.krew";
    };

    home.sessionPath = [
      "${config.home.homeDirectory}/.go/bin"
      "${config.home.homeDirectory}/.krew/bin"
    ];

    programs.zsh = {
      enable = true;
      dotDir = "${config.home.homeDirectory}/.config/zsh";
      shellAliases = {
        cat = "bat";
        yq4 = "yq";
      };
      initContent = ''
        compdef kubecolor=kubectl
      '';
      oh-my-zsh = {
        enable = true;
        theme = "robbyrussell";
        plugins = [
          "git"
          "fzf"
          "gh"
          "helm"
          "golang"
          "terraform"
          "aws"
          "pip"
          "python"
          "docker"
          "zoxide"
          "eza"
          "azure"
        ];
      };
    };
  };
}
