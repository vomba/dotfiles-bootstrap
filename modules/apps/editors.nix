{
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.apps.editors.enable {
    programs.helix = {
      enable = true;
      defaultEditor = true;

      languages = {
        language-server = {
          nixd.command = "nixd";
          bash-language-server = {
            command = "bash-language-server";
            args = [ "start" ];
          };
          yaml-language-server = {
            command = "yaml-language-server";
            args = [ "--stdio" ];
          };
          vscode-json-language-server = {
            command = "vscode-json-languageserver";
            args = [ "--stdio" ];
          };
          terraform-ls = {
            command = "terraform-ls";
            args = [ "serve" ];
          };
          helm-ls = {
            command = "helm_ls";
            args = [ "serve" ];
          };
          gopls.command = "gopls";
          marksman.command = "marksman";
          mpls = {
            command = "mpls";
            args = [
              "--dark-mode"
              "--enable-emoji"
            ];
          };
        };

        language = [
          {
            name = "nix";
            language-servers = [ "nixd" ];
            auto-format = true;
            formatter.command = "nixfmt";
          }
          {
            name = "bash";
            language-servers = [ "bash-language-server" ];
          }
          {
            name = "yaml";
            language-servers = [ "yaml-language-server" ];
          }
          {
            name = "json";
            language-servers = [ "vscode-json-language-server" ];
          }
          {
            name = "hcl";
            language-servers = [ "terraform-ls" ];
            language-id = "terraform";
          }
          {
            name = "tfvars";
            language-servers = [ "terraform-ls" ];
            language-id = "terraform-vars";
          }
          {
            name = "helm";
            language-servers = [ "helm-ls" ];
          }
          {
            name = "go";
            language-servers = [ "gopls" ];
          }
          {
            name = "markdown";
            language-servers = [
              "marksman"
              "mpls"
            ];
          }
        ];
      };
    };
  };
}
