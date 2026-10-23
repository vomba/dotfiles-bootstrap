{
  config,
  lib,
  pkgs,
  ...
}:
{
  imports = [ ./completions.nix ];

  config = lib.mkIf config.dotfiles.shell.zsh.enable {
    # ksync calls its packaged helper directly; no vendor bin directory in PATH.
    home.packages = with pkgs; [
      bash
      kubectl
      rsync
    ];

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
      enableCompletion = true;
      dotDir = "${config.home.homeDirectory}/.config/zsh";
      shellAliases = {
        cat = "bat";
        yq4 = "yq";
        tree = "eza --tree";
        watch = "viddy";
        top = "btop";
        du = "dust";
        df = "duf";
      };
      initContent = ''
        compdef kubecolor=kubectl
      '';
      oh-my-zsh = {
        enable = true;
        theme = "robbyrussell";
        custom = "${pkgs.zsh-kubecolor}/share/oh-my-zsh/custom";
        plugins = [
          "git"
          "fzf"
          "gh"
          "helm"
          "zsh-kubecolor"
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
    # Oh My Zsh owns the sole fzf Zsh integration.
    programs.fzf.enableZshIntegration = lib.mkForce false;
  };
}
