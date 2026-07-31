{ super }:

super.helmfile.overrideAttrs (_oldAttrs: rec {
  pname = "helmfile";
  version = "1.7.1";

  src = super.fetchFromGitHub {
    owner = "helmfile";
    repo = "helmfile";
    rev = "v${version}";
    hash = "sha256-RfhOc/iraWHdccKE5rp7WR5s9HhLOmxtSe1SZXy2UMI=";
  };

  vendorHash = "sha256-Q+G4G27hwT+zravktjk+mU0VC3OlS9zqXwclwACG9B0=";

  ldflags = [
    "-s"
    "-w"
    "-X go.szostok.io/version.version=v${version}"
    "-buildid="
  ];
})
