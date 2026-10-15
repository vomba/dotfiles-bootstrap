{ super }:

let
  helm = super.kubernetes-helm.overrideAttrs (_oldAttrs: rec {
    pname = "kubernetes-helm";
    version = "4.3.1";

    src = super.fetchFromGitHub {
      owner = "helm";
      repo = "helm";
      rev = "v${version}";
      hash = "sha256-XbRqZWsr012zT6dXumg4YShsjXeVNt6Qk52ttEFm/Oo=";
    };

    proxyVendor = true;
    vendorHash = "sha256-m919rjFcL0K8MX7M+1MYFu/V8SYCOzb/oy6scIzPXSk=";

    doCheck = false;

    ldflags = [
      "-w"
      "-s"
      "-X helm.sh/helm/v4/internal/version.version=v${version}"
      "-X helm.sh/helm/v4/internal/version.gitCommit=${src.rev}"
    ];

    preBuild = ''
      K8S_MODULES_VER="$(go list -f '{{.Version}}' -m k8s.io/client-go)"
      K8S_MODULES_MAJOR_VER="$(($(cut -d. -f1 <<<"$K8S_MODULES_VER") + 1))"
      K8S_MODULES_MINOR_VER="$(cut -d. -f2 <<<"$K8S_MODULES_VER")"
      ldflags="''${ldflags} -X helm.sh/helm/v4/pkg/lint/rules.k8sVersionMajor=''${K8S_MODULES_MAJOR_VER}"
      ldflags="''${ldflags} -X helm.sh/helm/v4/pkg/lint/rules.k8sVersionMinor=''${K8S_MODULES_MINOR_VER}"
      ldflags="''${ldflags} -X helm.sh/helm/v4/pkg/chartutil.k8sVersionMajor=''${K8S_MODULES_MAJOR_VER}"
      ldflags="''${ldflags} -X helm.sh/helm/v4/pkg/chartutil.k8sVersionMinor=''${K8S_MODULES_MINOR_VER}"
    '';
  });

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
