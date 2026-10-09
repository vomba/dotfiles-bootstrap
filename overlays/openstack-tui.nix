{
  lib,
  fetchurl,
  stdenv,
}:

let
  version = "0.13.8";

  # Map Nix system names to release artifact system names
  systemMap = {
    x86_64-linux = "x86_64-unknown-linux-gnu";
    aarch64-linux = "aarch64-unknown-linux-gnu";
    x86_64-darwin = "x86_64-apple-darwin";
    aarch64-darwin = "aarch64-apple-darwin";
  };

  system = systemMap.${stdenv.hostPlatform.system} or stdenv.hostPlatform.system;

  cli-src = fetchurl {
    url = "https://github.com/gtema/openstack/releases/download/v${version}/openstack_cli-${system}.tar.xz";
    sha256 =
      {
        "x86_64-unknown-linux-gnu" = "sha256-EsUuRXuDsNVMTFkTWDVk6gx0EU/+O0W3vPJQt7cvKUQ=";
        "aarch64-unknown-linux-gnu" = "sha256-cj7PCBMDW38WUG6BNQZsEbBWeLsIO69eIjn91WOmH5M=";
        "x86_64-apple-darwin" = "sha256-hPeqgUhBR7s2wCC/04nXEOekT3rS+prSEmux9xatxnM=";
        "aarch64-apple-darwin" = "sha256-Hn9gW3lNlITZkgFyFaUU+6f9eu6lgCD9aDD3NPxBhhY=";
      }
      .${system};
  };

  tui-src = fetchurl {
    url = "https://github.com/gtema/openstack/releases/download/v${version}/openstack_tui-${system}.tar.xz";
    sha256 =
      {
        "x86_64-unknown-linux-gnu" = "sha256-bg6TgDgSqBcOto+GG8qGy31HiJNGOvHlYJeC9K96w00=";
        "aarch64-unknown-linux-gnu" = "sha256-NTqHhMFL6jhe7UuA/F5hVnof6z7DuWAlx3Y4ZwA1/WE=";
        "x86_64-apple-darwin" = "sha256-w0Rv7BEcZDdgpiWKAombZxWEU/ccTqatDwNKuNqXdWw=";
        "aarch64-apple-darwin" = "sha256-wnk8bcjgIB0Re9xvsRxvAopoayn8F9Uv0uBELRPnY78=";
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
