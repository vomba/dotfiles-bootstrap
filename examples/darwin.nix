{ pkgs, self, ... }:
{
  users.users.bootstrap = {
    name = "bootstrap";
    home = "/Users/bootstrap";
    shell = pkgs.zsh;
  };

  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    users.bootstrap = {
      imports = [
        self.homeManagerModules.bootstrap
        ./darwin-home.nix
      ];
    };
  };
}
