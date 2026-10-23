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
      scriptTests =
        pkgs:
        pkgs.runCommand "bootstrap-script-tests"
          {
            nativeBuildInputs = with pkgs; [
              python3
              zsh
              bash
              coreutils
              gnugrep
              gnused
            ];
          }
          ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            cp -r ${self} source
            chmod -R u+w source
            patchShebangs --build source/scripts \
              source/oh-my-zsh/plugins/zsh-kubecolor/bin
            ${pkgs.python3}/bin/python3 -m unittest discover \
              -s source/tests/scripts -p 'test_*.py'
            touch "$out"
          '';
      nordProfile =
        pkgs:
        import ./tests/nord-profile.nix {
          inherit pkgs home-manager;
          bootstrapModule = self.homeManagerModules.bootstrap;
        };
      miseIntegration =
        pkgs:
        import ./tests/mise-integration.nix {
          inherit pkgs home-manager;
          bootstrapModule = self.homeManagerModules.bootstrap;
        };
      shellCompletions =
        pkgs:
        import ./tests/shell-completions.nix {
          inherit pkgs home-manager;
          bootstrapModule = self.homeManagerModules.bootstrap;
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
          python3
          zsh
          bash
        ];
      };

      devShells.${darwinSystem}.default = (mkPkgs darwinSystem).mkShell {
        packages = with (mkPkgs darwinSystem); [
          nixfmt
          pre-commit
          shellcheck
          gitleaks
          python3
          zsh
          bash
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

      packages.${linuxSystem} = {
        example-linux = self.homeConfigurations."example-linux".activationPackage;
        mise = (mkPkgs linuxSystem).mise;
        zsh-kubecolor = (mkPkgs linuxSystem).zsh-kubecolor;
      };
      packages.${darwinSystem} = {
        example-darwin = self.darwinConfigurations."example-darwin".system;
        mise = (mkPkgs darwinSystem).mise;
        zsh-kubecolor = (mkPkgs darwinSystem).zsh-kubecolor;
      };

      checks.${linuxSystem} = {
        example-linux = self.packages.${linuxSystem}.example-linux;
        script-tests = scriptTests (mkPkgs linuxSystem);
        nord-profile = nordProfile (mkPkgs linuxSystem);
        mise-integration = miseIntegration (mkPkgs linuxSystem);
        shell-completions = shellCompletions (mkPkgs linuxSystem);
        zsh-kubecolor = self.packages.${linuxSystem}.zsh-kubecolor;
      };
      checks.${darwinSystem} = {
        example-darwin = self.packages.${darwinSystem}.example-darwin;
        script-tests = scriptTests (mkPkgs darwinSystem);
        nord-profile = nordProfile (mkPkgs darwinSystem);
        mise-integration = miseIntegration (mkPkgs darwinSystem);
        shell-completions = shellCompletions (mkPkgs darwinSystem);
        zsh-kubecolor = self.packages.${darwinSystem}.zsh-kubecolor;
      };
    };
}
