{
  lib,
  stdenvNoCC,
  fetchurl,
  fetchFromGitHub,
  installShellFiles,
  testers,
  nix-update-script,
  mise,
}:
let
  version = "2026.10.17";
  releases = {
    x86_64-linux = {
      platform = "linux-x64-musl";
      hash = "sha256-2g2azl1bMyHjkfFfv4tT/NZbkCTmc+pRxVFRieWBBBY=";
    };
    aarch64-darwin = {
      platform = "macos-arm64";
      hash = "sha256-jrR25W9oCpNHzik0Z2jYQXT2MaN+Axd1ni4qVlgwyl0=";
    };
  };
  release = releases.${stdenvNoCC.hostPlatform.system};
  completionSource = fetchFromGitHub {
    owner = "jdx";
    repo = "mise";
    tag = "v${version}";
    hash = "sha256-3JBs2i+07hY27EpIrmbMu7D5p6rGV6wuerilx0BECjw=";
  };
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "mise";
  inherit version;
  src = fetchurl {
    url = "https://github.com/jdx/mise/releases/download/v${version}/mise-v${version}-${release.platform}.tar.xz";
    inherit (release) hash;
  };
  nativeBuildInputs = [ installShellFiles ];
  dontConfigure = true;
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    install -Dm755 bin/mise "$out/bin/mise"
    installManPage man/man1/mise.1
    installShellCompletion \
      --bash ${completionSource}/completions/mise.bash \
      --fish ${completionSource}/completions/mise.fish \
      --zsh ${completionSource}/completions/_mise
    mkdir -p "$out/lib/mise"
    touch "$out/lib/mise/.disable-self-update"
    runHook postInstall
  '';
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    export HOME="$TMPDIR/version-home"
    mkdir -p "$HOME"
    "$out/bin/mise" --version | grep -F '${version}'
    runHook postInstallCheck
  '';
  passthru = {
    updateScript = nix-update-script { };
    tests.version = testers.testVersion { package = finalAttrs.finalPackage; };
  };
  meta = mise.meta // {
    description = "Development tool manager and task runner (official release binary)";
    changelog = "https://github.com/jdx/mise/releases/tag/v${version}";
    platforms = builtins.attrNames releases;
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
