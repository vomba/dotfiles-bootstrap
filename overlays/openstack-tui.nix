{
  lib,
  fetchurl,
  stdenv,
}:

let
  version = "0.13.7";
  systemMap = {
    x86_64-linux = "x86_64-unknown-linux-gnu";
    aarch64-linux = "aarch64-unknown-linux-gnu";
    x86_64-darwin = "x86_64-apple-darwin";
    aarch64-darwin = "aarch64-apple-darwin";
  };
  system = systemMap.${stdenv.system} or stdenv.system;

  cli-src = fetchurl {
    url = "https://github.com/gtema/openstack/releases/download/v${version}/openstack_cli-${system}.tar.xz";
    sha256 =
      {
        "x86_64-unknown-linux-gnu" = "090lbzqyakykh5d1vwfps2wa9yp7p8zga40isx17hgdkbs97s6j3";
        "aarch64-unknown-linux-gnu" = "60fcd7a04152e24b6a14563b8f00c180adeadb262b6a91fad36340fa67df644f";
        "x86_64-apple-darwin" = "976fc7135bfca4ba18e784aa711fe88de7d99d8acd5b385cfc07edfae0a4575d";
        "aarch64-apple-darwin" = "fce8550a2c68789104c8e32e07c4484877e3ca6e56db09a430a3c4b316757ed7";
      }
      .${system};
  };

  tui-src = fetchurl {
    url = "https://github.com/gtema/openstack/releases/download/v${version}/openstack_tui-${system}.tar.xz";
    sha256 =
      {
        "x86_64-unknown-linux-gnu" = "11hda0zkaaiim7amx118xka5qy4pzh5chl56w35ac83q0qh0nrb4";
        "aarch64-unknown-linux-gnu" = "2869e69419a204819c94dd7783351198da9f7cc99c04b63bef31e0715a317d3f";
        "x86_64-apple-darwin" = "0efbb9f59f1eec93d5fce6f8b3871f2c5ed968c0f702f9b2caf66d2f22f7de46";
        "aarch64-apple-darwin" = "2febb9fb88b31a3d3d39de92c987efbb0428408bc4bb7cb07ae1231974e874cb";
      }
      .${system};
  };
in
stdenv.mkDerivation {
  pname = "openstack-tui";
  inherit version;
  src = cli-src;

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    mkdir -p $out/bin
    tar -xf ${cli-src} --strip-components=1 -C $out/bin
    tar -xf ${tui-src} --strip-components=1 -C $out/bin
  '';

  meta = with lib; {
    description = "OpenStack CLI and TUI client (Rust)";
    homepage = "https://github.com/gtema/openstack";
    license = licenses.asl20;
    maintainers = [ ];
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
      "x86_64-darwin"
      "aarch64-darwin"
    ];
  };
}
