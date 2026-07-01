let
  defaultPkgsConfig = {
    allowUnfree = true;
    allowUnfreePredicate = _: true;
  };
in
{
  inherit defaultPkgsConfig;

  mkPkgs =
    {
      nixpkgs,
      system,
      overlays ? [ ],
      config ? defaultPkgsConfig,
    }:
    import nixpkgs {
      inherit system overlays config;
    };

  mkHomeConfiguration =
    {
      home-manager,
      nixpkgs,
      system,
      overlays ? [ ],
      pkgsConfig ? defaultPkgsConfig,
      extraSpecialArgs ? { },
      modules ? [ ],
    }:
    let
      pkgs = import nixpkgs {
        inherit system overlays;
        config = pkgsConfig;
      };
    in
    home-manager.lib.homeManagerConfiguration {
      inherit pkgs extraSpecialArgs modules;
    };

  mkDarwinConfiguration =
    {
      nix-darwin,
      nixpkgs,
      system,
      overlays ? [ ],
      pkgsConfig ? defaultPkgsConfig,
      specialArgs ? { },
      modules ? [ ],
    }:
    let
      pkgs = import nixpkgs {
        inherit system overlays;
        config = pkgsConfig;
      };
    in
    nix-darwin.lib.darwinSystem {
      inherit pkgs specialArgs modules;
    };
}
