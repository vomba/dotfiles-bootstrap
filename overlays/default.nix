final: prev: {
  mise = prev.callPackage ../packages/mise.nix { mise = prev.mise; };
  openstack-tui = prev.callPackage ./openstack-tui.nix { };
  # GTK 3/4 CSS themes and icons; no retired GTK 2 Murrine dependency.
  nordic = final.callPackage ./nordic.nix { };
  zsh-kubecolor = import ./zsh-kubecolor.nix { super = final; };
  kubernetes-helm = import ./helm.nix { super = prev; };
}
