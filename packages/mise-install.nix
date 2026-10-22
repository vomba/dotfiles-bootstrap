{
  lib,
  writeTextFile,
  linkFarm,
  runCommand,
  bash,
  coreutils,
  git,
  cacert,
  shellcheck,
  mise,
  configFile,
  lockFile,
  commands,
}:
let
  installerConfig = linkFarm "dotfiles-mise-install-config" [
    {
      name = "config.toml";
      path = configFile;
    }
    {
      name = "mise.lock";
      path = lockFile;
    }
  ];
  emptySystemConfigDir = runCommand "dotfiles-mise-empty-system-config" { } ''
    mkdir -p "$out"
  '';
  emptySystemConfig = writeTextFile {
    name = "dotfiles-mise-empty-system.toml";
    text = "";
  };
  miseBin = "${mise}/bin/mise";
  installerPath = lib.makeBinPath [
    mise
    coreutils
    bash
    git
  ];
in
writeTextFile {
  name = "dotfiles-mise-install";
  executable = true;
  destination = "/bin/dotfiles-mise-install";
  checkPhase = ''
    ${bash}/bin/bash -n "$target"
    ${shellcheck}/bin/shellcheck --shell=bash "$target"
  '';
  text = ''
    #!${bash}/bin/bash -p
    set -euo pipefail

    if (( $# != 0 )); then
      printf '%s\n' 'usage: dotfiles-mise-install' >&2
      exit 2
    fi
    if [[ ''${HOME-} != /* ]]; then
      printf '%s\n' 'dotfiles-mise-install: HOME must be nonempty and absolute' >&2
      exit 2
    fi

    XDG_CONFIG_HOME=''${XDG_CONFIG_HOME-"$HOME/.config"}
    XDG_DATA_HOME=''${XDG_DATA_HOME-"$HOME/.local/share"}
    XDG_CACHE_HOME=''${XDG_CACHE_HOME-"$HOME/.cache"}
    XDG_STATE_HOME=''${XDG_STATE_HOME-"$HOME/.local/state"}
    for variable in XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME; do
      if [[ ''${!variable} != /* ]]; then
        printf 'dotfiles-mise-install: %s must be nonempty and absolute\n' "$variable" >&2
        exit 2
      fi
    done

    # The privileged Bash shebang suppresses inherited startup code before this
    # sanitized child environment can be assembled.
    mise_environment=(
      ${coreutils}/bin/env -i
      "HOME=$HOME"
      "XDG_CONFIG_HOME=$XDG_CONFIG_HOME"
      "XDG_DATA_HOME=$XDG_DATA_HOME"
      "XDG_CACHE_HOME=$XDG_CACHE_HOME"
      "XDG_STATE_HOME=$XDG_STATE_HOME"
      LC_ALL=C
      SSL_CERT_FILE=${cacert}/etc/ssl/certs/ca-bundle.crt
      PATH=${installerPath}
      MISE_CONFIG_DIR=${installerConfig}
      MISE_SYSTEM_CONFIG_DIR=${emptySystemConfigDir}
      MISE_GLOBAL_CONFIG_FILE=${installerConfig}/config.toml
      MISE_SYSTEM_CONFIG_FILE=${emptySystemConfig}
      MISE_CEILING_PATHS=/
      MISE_NO_ENV=1
      MISE_NO_HOOKS=1
      MISE_SAFE=1
      MISE_LOCKED=1
      "MISE_SHIMS_DIR=$XDG_DATA_HOME/mise-shims"
    )

    # Early .miserc.toml discovery happens before mise processes its -C flag.
    cd /
    "''${mise_environment[@]}" MISE_OFFLINE=0 ${miseBin} -C / install --locked
    "''${mise_environment[@]}" MISE_OFFLINE=1 ${miseBin} -C / reshim --force

    # Shared-directory ownership is mise's responsibility. It preserves foreign
    # collisions; refuse success unless every selected shim has the exact target.
    managed_commands=( ${lib.escapeShellArgs commands} )
    for command in "''${managed_commands[@]}"; do
      shim="$XDG_DATA_HOME/mise-shims/$command"
      if [[ ! -L "$shim" ]] || [[ "$(${coreutils}/bin/readlink -- "$shim")" != ${lib.escapeShellArg miseBin} ]]; then
        printf 'dotfiles-mise-install: refusing foreign shim: %s\n' "$shim" >&2
        exit 1
      fi
    done
  '';
}
