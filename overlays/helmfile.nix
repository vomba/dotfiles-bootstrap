{ super }:

super.helmfile.overrideAttrs (_oldAttrs: rec {
  pname = "helmfile";
  version = "1.6.0";

  src = super.fetchFromGitHub {
    owner = "helmfile";
    repo = "helmfile";
    rev = "v${version}";
    hash = "sha256-rv7C/2CExlMO6fXaMMMAgSxqKP5iwLyMFI2huHeFVe0=";
  };

  vendorHash = "sha256-uHzDxhJynjijm6dXW9fgiLilxUkch/IBmtQpOXTvA9M=";

  ldflags = [
    "-s"
    "-w"
    "-X go.szostok.io/version.version=v${version}"
    "-buildid="
  ];
})
