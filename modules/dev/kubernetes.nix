{
  pkgs,
  config,
  lib,
  ...
}:
let
  cfg = config.dotfiles.dev.kubernetes;
  helmWithPlugins = pkgs.kubernetes-helm.withPlugins (p: [
    p.helm-diff
    p.helm-secrets
    p.helm-secrets-getter
    p.helm-secrets-post-renderer
  ]);
  helmPluginsPath = "${helmWithPlugins}";
in
{
  config = lib.mkIf cfg.enable {
    home.packages = [
      pkgs.kubie
      pkgs.kind
      helmWithPlugins
      pkgs.helmfile
      pkgs.kubecolor
      pkgs.kubectl
      pkgs.kubelogin-oidc
      pkgs.sonobuoy
      pkgs.velero
      pkgs.crossplane-cli
      pkgs.kubectl-view-secret
      pkgs.viddy
    ];

    home.file.".kube/kubie.yaml" = lib.mkIf (cfg.kubieConfigPath != null) {
      source = cfg.kubieConfigPath;
    };

    home.activation.helmPluginsLink = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      $DRY_RUN_CMD mkdir -p $VERBOSE_ARG "''${HOME}/.local/share/helm"
      if [ -e "''${HOME}/.local/share/helm/plugins" ]; then
        $DRY_RUN_CMD rm -f $VERBOSE_ARG "''${HOME}/.local/share/helm/plugins"
      fi
      $DRY_RUN_CMD ln -sf $VERBOSE_ARG "${helmPluginsPath}" "''${HOME}/.local/share/helm/plugins"
    '';
  };
}
