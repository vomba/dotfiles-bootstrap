{ super }:

super.helmfile.overrideAttrs (_oldAttrs: rec {
  pname = "helmfile";
  version = "1.8.0";

  src = super.fetchFromGitHub {
    owner = "helmfile";
    repo = "helmfile";
    rev = "v${version}";
    hash = "sha256-sPzJ/DniVFCY4hrZvLXpvcFFaT7vdTWiwxoqly7hzOs=";
  };

  vendorHash = "sha256-bpiIOfh6hvaAS+hcwd4phv8fXwzbdUCYVxZyyejTx8k=";

  ldflags = [
    "-s"
    "-w"
    "-X go.szostok.io/version.version=v${version}"
    "-buildid="
  ];
})
