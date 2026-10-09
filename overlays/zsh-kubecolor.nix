{ super }:

super.stdenvNoCC.mkDerivation {
  pname = "zsh-kubecolor";
  version = "2026-10-09";

  src = ../oh-my-zsh/plugins/zsh-kubecolor;
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    plugin="$out/share/oh-my-zsh/custom/plugins/zsh-kubecolor"
    mkdir -p "$plugin"
    cp -R . "$plugin/"
    chmod -R u+w "$plugin"
    chmod +x "$plugin/bin/kubectl-rsync"

    # This does not read kubeconfig or contact a cluster.
    ${super.kubectl}/bin/kubectl completion zsh > "$plugin/completion/kubectl_completion"

    # Keep dependencies store-backed without exporting the helper directory.
    substituteInPlace "$plugin/bin/kubectl-rsync" \
      --replace-fail '#!/usr/bin/env bash' '#!${super.bash}/bin/bash' \
      --replace-fail 'exec rsync ' 'exec ${super.rsync}/bin/rsync ' \
      --replace-fail 'exec kubectl ' 'exec ${super.kubectl}/bin/kubectl '

    runHook postInstall
  '';

  meta = {
    description = "Kubernetes Zsh aliases, pod picker and rsync helper";
    license = super.lib.licenses.mit;
    platforms = super.lib.platforms.unix;
  };
}
