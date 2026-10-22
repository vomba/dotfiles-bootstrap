{ super }:

let
  helm = super.kubernetes-helm;

  buildHelmPlugin =
    {
      pluginName,
      version,
      src,
      postPatch ? "",
      preFixup ? "",
      ...
    }@args:
    super.stdenv.mkDerivation (
      args
      // {
        pname = pluginName;
        inherit
          version
          src
          postPatch
          preFixup
          ;
        dontBuild = true;
        installPhase =
          args.installPhase or ''
            runHook preInstall
            install -d $out/${pluginName}
            cp -r * $out/${pluginName}/
            runHook postInstall
          '';
      }
    );

  customHelmPlugins = super.kubernetes-helmPlugins // {
    helm-secrets = buildHelmPlugin rec {
      pluginName = "helm-secrets";
      version = "4.7.9";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-${version}.tgz";
        hash = "sha256-W5zEcV9zySdJOltAE0KmpyxCO/WLQXX2RC6xEaHafWg=";
      };
    };

    helm-secrets-getter = buildHelmPlugin rec {
      pluginName = "helm-secrets-getter";
      version = "4.7.9";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-getter-${version}.tgz";
        hash = "sha256-liON1ogfzXU8ERI6hsizCMYm3L8anp5tC32hbHlaC+s=";
      };
    };

    helm-secrets-post-renderer = buildHelmPlugin rec {
      pluginName = "helm-secrets-post-renderer";
      version = "4.7.9";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-post-renderer-${version}.tgz";
        hash = "sha256-nLnXLumC/PFPREWec/MXBXX01hIUjmUdD/GiOsL+yoo=";
      };
    };
  };
in
helm.overrideAttrs (oldAttrs: {
  passthru = (oldAttrs.passthru or { }) // {
    withPlugins =
      pluginsFn:
      super.wrapHelm helm {
        plugins = pluginsFn customHelmPlugins;
      };
    inherit buildHelmPlugin customHelmPlugins;
  };
})
