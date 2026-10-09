final: prev:
(import ./languages.nix final prev)
// {
  cidr = prev.callPackage ./cidr.nix { };
  openstack-tui = prev.callPackage ./openstack-tui.nix { };
  # GTK 3/4 CSS themes and icons; no retired GTK 2 Murrine dependency.
  nordic = final.callPackage ./nordic.nix { };
  zsh-kubecolor = import ./zsh-kubecolor.nix { super = final; };
  kubernetes-helm = import ./helm.nix { super = prev; };
  helmfile = import ./helmfile.nix { super = prev; };
  yq-go = import ./yq.nix { super = prev; };
  sops = import ./sops.nix { super = prev; };
}
