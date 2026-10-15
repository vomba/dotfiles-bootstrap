{ super }:

(super.helmfile.override { buildGoModule = super.buildGo127Module; }).overrideAttrs
  (_oldAttrs: rec {
    pname = "helmfile";
    version = "1.8.1";

    src = super.fetchFromGitHub {
      owner = "helmfile";
      repo = "helmfile";
      rev = "v${version}";
      hash = "sha256-G9gOc7gIj2AoROPRppHqkeGcVUOpYx/qaEVkec4c4rk=";
    };

    vendorHash = "sha256-GBTUYoXCcRD6kZI52VWE46GDlO13ZpxDEYrDd7nRR3s=";

    ldflags = [
      "-s"
      "-w"
      "-X go.szostok.io/version.version=v${version}"
      "-buildid="
    ];
  })
