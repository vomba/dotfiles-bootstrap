{
  pkgs,
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.apps.zed.enable {
    programs.zed-editor = {
      enable = true;
      package = if pkgs.stdenv.isLinux then pkgs.zed-editor-fhs else pkgs.zed-editor;
      enableMcpIntegration = true;

      extensions = [
        "nix"
        "go"
        "docker"
        "docker-compose"
        "terraform"
        "helm"
        "toml"
        "yaml"
        "json"
        "markdown-preview"
        "catppuccin"
      ];

      userSettings = {
        theme = "Catppuccin Mocha";
        ui_font_size = 14;
        buffer_font_size = 14;
        buffer_font_family = "JetBrains Mono";
        relative_line_numbers = true;
        cursor_blink = true;
        tab_size = 2;
        soft_wrap = "editor_width";
        format_on_save = "on";
        vim_mode = false;
        git.builtin = false;
        features = {
          copilot = false;
          inline_completion_provider = "none";
        };
        telemetry.metrics = false;
      };

      extraPackages = with pkgs; [
        nixd
        gopls
        marksman
        yaml-language-server
        vscode-json-languageserver
        terraform-ls
        helm-ls
        bash-language-server
        openssh
      ];

      defaultEditor = false;
    };
  };
}
