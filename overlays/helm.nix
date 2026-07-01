{ super }:

let
  helm = super.kubernetes-helm.overrideAttrs (_oldAttrs: rec {
    pname = "kubernetes-helm";
    version = "4.2.2";

    src = super.fetchFromGitHub {
      owner = "helm";
      repo = "helm";
      rev = "v${version}";
      hash = "sha256-q5s/d351Vcs9s2nhwr6nzwJYOnsn/CmrU6cGDFaHDcg=";
    };

    proxyVendor = true;
    vendorHash = "sha256-Xst69X7tqIsiABItWFQRHz4qmiUxRyPQttPqmX1Ko1w=";

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
      version = "4.7.7";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-${version}.tgz";
        hash = "sha256-qi/nMXoLY7s3gWjFCtq50uU5dvcFI0odDtz40MtSJY4=";
      };
    };

    helm-secrets-getter = buildHelmPlugin rec {
      pluginName = "helm-secrets-getter";
      version = "4.7.7";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-getter-${version}.tgz";
        hash = "sha256-GE4cF6KkJvheHI96ajIeyyzpx5f/3q7f22mXDRLEoNI=";
      };
    };

    helm-secrets-post-renderer = buildHelmPlugin rec {
      pluginName = "helm-secrets-post-renderer";
      version = "4.7.7";
      src = super.fetchurl {
        url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/secrets-post-renderer-${version}.tgz";
        hash = "sha256-1OgHPgh0Ix4nd6vLSq/TpkuIgn3FDYy0zQ+4t8wd7Gg=";
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
