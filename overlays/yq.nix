{ super }:

super.yq-go.overrideAttrs (_oldAttrs: rec {
  pname = "yq-go";
  version = "4.45.1";
  src = super.fetchFromGitHub {
    owner = "mikefarah";
    repo = "yq";
    rev = "v${version}";
    hash = "sha256-AsTDbeRMb6QJE89Z0NGooyTY3xZpWFoWkT7dofsu0DI=";
  };

  vendorHash = "sha256-d4dwhZYzEuyh1zJQ2xU0WkygHjoVLoCBrDKuAHUzu1w=";
})
