self: super:
(import ./languages.nix self super)
// {
  cidr = super.callPackage ./cidr.nix { };
  openstack-tui = super.callPackage ./openstack-tui.nix { };
  kubernetes-helm = import ./helm.nix { inherit super; };
  helmfile = import ./helmfile.nix { inherit super; };
  yq-go = import ./yq.nix { inherit super; };
  sops = import ./sops.nix { inherit super; };
}
