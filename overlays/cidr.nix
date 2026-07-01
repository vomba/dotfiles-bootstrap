{
  lib,
  buildGoModule,
  fetchFromGitHub,
}:

buildGoModule rec {
  pname = "cidr";
  version = "2.3.0";

  src = fetchFromGitHub {
    owner = "bschaatsbergen";
    repo = "cidr";
    rev = "v${version}";
    sha256 = "sha256-h7/38Zr1w/fomLnsSbO/UqR9g6koZkxiYk9CaKDdOVk=";
  };

  vendorHash = "sha256-tJ0IN0ujw4wFbMd4tgW8ugwyepHNqcSDFHnp5MyCbTg=";

  meta = with lib; {
    description = "Simple command line tool to merge ip/ip cidr/ip range, supports IPv4/IPv6";
    mainProgram = "cidr";
    homepage = "https://github.com/bschaatsbergen/cidr";
    license = licenses.mit;
    maintainers = [ ];
  };
}
