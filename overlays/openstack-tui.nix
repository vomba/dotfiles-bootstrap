{
  lib,
  fetchurl,
  stdenv,
}:

let
  version = "0.13.7";

  # Map Nix system names to release artifact system names
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
        "x86_64-unknown-linux-gnu" = "b4bfe560880b13bc5256246f9010e55a4362897e001329fd211f8755e6b907bc";
        "aarch64-unknown-linux-gnu" = "7368e79734d99af2c81eb7c96df6b440d1fee9837d5efbd4e5ca82fe34e230a3";
        "x86_64-apple-darwin" = "014c73006ed050cd252aac1a9507c73791ca7421043918f47a12e1d57c9a0c1d";
        "aarch64-apple-darwin" = "201ef11b29f23425dc435c8dc9d58401e4dfe61cf8d6bb486ca42a26cf896cca";
      }
      .${system};
  };

  tui-src = fetchurl {
    url = "https://github.com/gtema/openstack/releases/download/v${version}/openstack_tui-${system}.tar.xz";
    sha256 =
      {
        "x86_64-unknown-linux-gnu" = "6e1020bd8934af8eb4d82af9cdcd10830e4bb71bf5c2e768848d3a47c75bfdd9";
        "aarch64-unknown-linux-gnu" = "aceabdbd53cd853f1bab8e03294f415479af752a85f79d22654581603d4b6c5c";
        "x86_64-apple-darwin" = "9efc7d4cde27ec404066e5ca6713319db274abb2d3546a2ace3be260298978b2";
        "aarch64-apple-darwin" = "338e35ebd74ca48464a529d9db8bc037d137b42c9408bb0462fbd1c91f6bb218";
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
