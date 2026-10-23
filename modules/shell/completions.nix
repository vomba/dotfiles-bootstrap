{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.dotfiles;
  lazygitPackage =
    if config.programs.lazygit.enable && config.programs.lazygit.package != null then
      config.programs.lazygit.package
    else
      pkgs.lazygit;
  manifest = builtins.fromTOML (builtins.readFile ../../apps/mise/mise.toml);
  lock = builtins.fromTOML (builtins.readFile ../../apps/mise/mise.lock);
  platform =
    (if pkgs.stdenv.hostPlatform.isDarwin then "macos" else "linux")
    + (if pkgs.stdenv.hostPlatform.isAarch64 then "-arm64" else "-x64");
  helmfileLock =
    lib.findSingle
      (entry: entry.version == manifest.tools.helmfile && entry.backend == manifest.tool_alias.helmfile)
      (throw "helmfile completion: manifest version/backend missing from mise.lock")
      (throw "helmfile completion: ambiguous mise.lock entries")
      lock.tools.helmfile;
  helmfileArtifact = helmfileLock.${"platforms.${platform}"};
  # This exact mise-owned executable is a build/test input, never a home package.
  helmfileGenerator =
    pkgs.runCommand "helmfile-completion-generator-${helmfileLock.version}"
      {
        src = pkgs.fetchurl {
          inherit (helmfileArtifact) url;
          sha256 = lib.removePrefix "sha256:" helmfileArtifact.checksum;
        };
        nativeBuildInputs = [
          pkgs.gnutar
          pkgs.gzip
        ];
      }
      ''
        mkdir -p "$out/bin"
        tar -xzf "$src" -C "$out/bin" helmfile
        chmod +x "$out/bin/helmfile"
      '';
  executables =
    lib.optionalAttrs cfg.apps.enable {
      act = lib.getExe pkgs.act;
      golazo = lib.getExe pkgs.golazo;
    }
    // lib.optionalAttrs cfg.dev.cloud.enable {
      upctl = "${pkgs.upcloud-cli}/bin/upctl";
    }
    // lib.optionalAttrs cfg.dev.kubernetes.enable {
      sonobuoy = lib.getExe pkgs.sonobuoy;
      kubectl-oidc_login = "${pkgs.kubelogin-oidc}/bin/kubectl-oidc_login";
    }
    // lib.optionalAttrs (cfg.dev.kubernetes.enable && config.programs.mise.enable) {
      helmfile = "${helmfileGenerator}/bin/helmfile";
    }
    // lib.optionalAttrs (cfg.shell.git.enable || config.programs.lazygit.enable) {
      lazygit = lib.getExe lazygitPackage;
    };
  completions =
    pkgs.runCommand "dotfiles-zsh-completions"
      {
        nativeBuildInputs = [ pkgs.coreutils ];
        passthru = { inherit executables; };
      }
      ''
        mkdir -p "$out/share/zsh/site-functions" "$TMPDIR/home" "$TMPDIR/config" \
          "$TMPDIR/cache" "$TMPDIR/data" "$TMPDIR/state" "$TMPDIR/runtime"
        ${lib.concatStringsSep "\n" (
          lib.mapAttrsToList (name: executable: ''
            ${pkgs.coreutils}/bin/env -i HOME="$TMPDIR/home" \
              XDG_CONFIG_HOME="$TMPDIR/config" XDG_CACHE_HOME="$TMPDIR/cache" \
              XDG_DATA_HOME="$TMPDIR/data" XDG_STATE_HOME="$TMPDIR/state" \
              XDG_RUNTIME_DIR="$TMPDIR/runtime" TMPDIR="$TMPDIR" LC_ALL=C \
              SHELL=${pkgs.zsh}/bin/zsh PATH=${lib.makeBinPath [ pkgs.coreutils ]} \
              ${executable} completion zsh > "$out/share/zsh/site-functions/_${name}"
          '') executables
        )}
        ${lib.optionalString cfg.dev.kubernetes.enable ''
          # Nixpkgs renames int128/kubelogin; keep its native protocol on that name,
          # not the unrelated Azure kubelogin command.
          substituteInPlace "$out/share/zsh/site-functions/_kubectl-oidc_login" \
            --replace-fail kubelogin kubectl-oidc_login
        ''}
        ${lib.optionalString (builtins.hasAttr "lazygit" executables) ''
          # Upstream emits a sourceable script. Dispatch its newly defined
          # function on the first fpath autoload, not only on the second Tab.
          cat >> "$out/share/zsh/site-functions/_lazygit" <<'ZSH'
          if [[ $funcstack[1] == _lazygit ]]; then
            _lazygit "$@"
          fi
          ZSH
        ''}
      '';
in
{
  config = lib.mkIf cfg.shell.zsh.enable {
    home.packages = [
      (lib.setPrio 9 completions)
      (lib.lowPrio pkgs.zsh-completions)
    ];
    programs.zsh.initContent = lib.mkOrder 1200 (
      lib.optionalString cfg.dev.kubernetes.enable ''
        # Crossplane's upstream kongplete protocol uses Zsh's Bash compatibility.
        # Register it directly: its installer invokes unsupported macOS login lookup.
        autoload -U +X bashcompinit
        bashcompinit
        complete -C crossplane crossplane
      ''
      + lib.optionalString (config.programs.yazi.enable && config.programs.yazi.enableZshIntegration) ''
        compdef ${lib.escapeShellArg config.programs.yazi.shellWrapperName}=yazi
      ''
      +
        lib.optionalString (config.programs.lazygit.enable && config.programs.lazygit.enableZshIntegration)
          ''
            compdef ${lib.escapeShellArg config.programs.lazygit.shellWrapperName}=lazygit
          ''
    );
  };
}
