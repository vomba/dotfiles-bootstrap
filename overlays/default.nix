final: prev:
(import ./languages.nix final prev)
// {
  cidr = prev.callPackage ./cidr.nix { };
  openstack-tui = prev.callPackage ./openstack-tui.nix { };
  kubernetes-helm = import ./helm.nix { super = prev; };
  helmfile = import ./helmfile.nix { super = prev; };
  yq-go = import ./yq.nix { super = prev; };
  sops = import ./sops.nix { super = prev; };
}
