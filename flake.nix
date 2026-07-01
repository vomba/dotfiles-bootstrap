{
  description = "Reusable public bootstrap for Home Manager and nix-darwin dotfiles";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
      nix-darwin,
      ...
    }:
    let
      bootstrapLib = import ./lib/default.nix;
      linuxSystem = "x86_64-linux";
      darwinSystem = "aarch64-darwin";
      sharedOverlays = [ self.overlays.default ];
      mkPkgs =
        system:
        bootstrapLib.mkPkgs {
          inherit nixpkgs system;
          overlays = sharedOverlays;
        };
    in
    {
      lib = bootstrapLib;

      overlays.default = import ./overlays/default.nix;

      homeManagerModules.bootstrap = import ./modules/home.nix;
      darwinModules.bootstrap = import ./modules/darwin.nix;

      formatter = {
        ${linuxSystem} = nixpkgs.legacyPackages.${linuxSystem}.nixfmt;
        ${darwinSystem} = nixpkgs.legacyPackages.${darwinSystem}.nixfmt;
      };

      devShells.${linuxSystem}.default = (mkPkgs linuxSystem).mkShell {
        packages = with (mkPkgs linuxSystem); [
          nixfmt
          pre-commit
          shellcheck
          gitleaks
        ];
      };

      devShells.${darwinSystem}.default = (mkPkgs darwinSystem).mkShell {
        packages = with (mkPkgs darwinSystem); [
          nixfmt
          pre-commit
          shellcheck
          gitleaks
        ];
      };

      homeConfigurations."example-linux" = bootstrapLib.mkHomeConfiguration {
        inherit home-manager nixpkgs;
        system = linuxSystem;
        overlays = sharedOverlays;
        modules = [
          self.homeManagerModules.bootstrap
          ./examples/linux.nix
        ];
      };

      darwinConfigurations."example-darwin" = bootstrapLib.mkDarwinConfiguration {
        inherit nix-darwin nixpkgs;
        system = darwinSystem;
        overlays = sharedOverlays;
        specialArgs = { inherit self; };
        modules = [
          { nixpkgs.hostPlatform = darwinSystem; }
          self.darwinModules.bootstrap
          home-manager.darwinModules.home-manager
          ./examples/darwin.nix
        ];
      };

      packages.${linuxSystem}.example-linux = self.homeConfigurations."example-linux".activationPackage;
      packages.${darwinSystem}.example-darwin = self.darwinConfigurations."example-darwin".system;

      checks.${linuxSystem}.example-linux = self.packages.${linuxSystem}.example-linux;
      checks.${darwinSystem}.example-darwin = self.packages.${darwinSystem}.example-darwin;
    };
}
