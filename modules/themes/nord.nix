{
  config,
  lib,
  pkgs,
  ...
}:
let
  assets = ../../themes/nord;
  glamourNord = assets + "/glamour-nord.json";
  # Generated from vivid-nord.yml with `vivid generate`; keep evaluation pure.
  lsColorsNord = builtins.readFile (assets + "/ls-colors-nord");
  defaults = lib.mapAttrsRecursive (_: lib.mkDefault);
in
{
  config = lib.mkIf config.dotfiles.theme.nord.enable (
    lib.mkMerge [
      (lib.mkIf config.dotfiles.shell.enable {
        programs.bat = {
          config.theme = lib.mkDefault "nord-contrast";
          themes.nord-contrast = defaults {
            src = assets;
            file = "Nord-contrast.tmTheme";
          };
        };

        programs.fzf.colors = defaults {
          fg = "#ECEFF4";
          bg = "#000000";
          hl = "#81A1C1";
          "fg+" = "#ECEFF4";
          "bg+" = "#3B4252";
          "hl+" = "#81A1C1";
          info = "#A3BE8C";
          prompt = "#5E81AC";
          pointer = "#B48EAD";
          marker = "#EBCB8B";
          spinner = "#81A1C1";
          header = "#5E81AC";
        };

        programs.btop = {
          enable = lib.mkDefault true;
          settings.color_theme = lib.mkDefault "nord-contrast";
        };
        xdg.configFile."btop/themes/nord-contrast.theme".source = lib.mkDefault (
          assets + "/btop-nord.theme"
        );

        # The shell baseline uses mkOptionDefault leaves; consumer settings win.
        programs.starship.settings = defaults (
          builtins.fromTOML (builtins.readFile (assets + "/starship.toml"))
        );

        home.sessionVariables = lib.mkMerge [
          (defaults {
            LS_COLORS = lsColorsNord;
            GLAMOUR_STYLE = "${glamourNord}";
          })
          (lib.mkIf config.programs.bat.enable {
            BAT_THEME = lib.mkDefault "nord-contrast";
          })
        ];
      })

      (lib.mkIf config.dotfiles.shell.git.enable {
        programs.delta.options = lib.mkMerge [
          (defaults {
            navigate = true;
            line-numbers = true;
            minus-style = "syntax #3b4252";
            minus-emph-style = "syntax #4c566a";
            plus-style = "syntax #2e3440";
            plus-emph-style = "syntax #434c5e";
          })
          (lib.mkIf (config.dotfiles.shell.enable && config.programs.bat.enable) {
            syntax-theme = lib.mkDefault "nord-contrast";
          })
        ];

        programs.lazygit = {
          enable = lib.mkDefault true;
          settings.gui.theme = defaults {
            activeBorderColor = [
              "#5E81AC"
              "bold"
            ];
            inactiveBorderColor = [ "#4C566A" ];
            searchingActiveBorderColor = [
              "#EBCB8B"
              "bold"
            ];
            optionsTextColor = [ "#81A1C1" ];
            selectedLineBgColor = [ "#434C5E" ];
            cherryPickedCommitFgColor = [ "#81A1C1" ];
            cherryPickedCommitBgColor = [ "#4C566A" ];
            markedBaseCommitFgColor = [ "#EBCB8B" ];
            markedBaseCommitBgColor = [ "#4C566A" ];
            unstagedChangesColor = [ "#BF616A" ];
            defaultFgColor = [ "#ECEFF4" ];
          };
        };
      })

      (lib.mkIf config.dotfiles.apps.editors.enable {
        programs.helix = {
          settings.theme = lib.mkDefault "nord-contrast";
          themes.nord-contrast = lib.mkDefault (assets + "/helix-nord-contrast.toml");
        };
      })

      (lib.mkIf config.dotfiles.apps.yazi.enable {
        programs.yazi = {
          flavors.nord = lib.mkDefault pkgs.yaziPlugins.nord;
          theme = defaults {
            flavor = {
              dark = "nord";
              light = "nord";
            };
            mgr = {
              cwd.fg = "#5E81AC";
              marker_marked = {
                fg = "#5E81AC";
                bg = "#5E81AC";
              };
              border_style.fg = "#7B88A1";
            };
            tabs = {
              active = {
                fg = "#ECEFF4";
                bg = "#5E81AC";
                bold = true;
              };
              inactive = {
                fg = "#81A1C1";
                bg = "#2E3440";
              };
            };
            mode = {
              normal_main = {
                fg = "#ECEFF4";
                bg = "#5E81AC";
                bold = true;
              };
              normal_alt = {
                fg = "#81A1C1";
                bg = "#2E3440";
              };
              select_main = {
                fg = "#ECEFF4";
                bg = "#4C566A";
                bold = true;
              };
              select_alt = {
                fg = "#81A1C1";
                bg = "#2E3440";
              };
            };
            which = {
              cand.fg = "#81A1C1";
              rest.fg = "#5E81AC";
            };
            help = {
              on.fg = "#81A1C1";
              footer = {
                fg = "#ECEFF4";
                bg = "#2E3440";
              };
            };
            pick = {
              border.fg = "#5E81AC";
              inactive.fg = "#ECEFF4";
            };
            input = {
              border.fg = "#5E81AC";
              title.fg = "#ECEFF4";
            };
            cmp.border.fg = "#5E81AC";
            tasks.border.fg = "#5E81AC";
          };
        };
      })

      (lib.mkIf config.dotfiles.apps.enable {
        xdg.configFile."glow/glow.yml".text = lib.mkDefault ''
          style: ${glamourNord}
        '';
      })
    ]
  );
}
