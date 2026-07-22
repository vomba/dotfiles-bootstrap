{
  pkgs,
  config,
  lib,
  ...
}:
{
  config = lib.mkIf config.dotfiles.shell.gnuTools.enable {
    # Full GNU userland on Darwin. Nix puts each tool at bin/<name> with no
    # prefix, so these shadow the matching BSD utilities in /usr/bin. No-op
    # on Linux (everything here is GNU already). `pkgs.bash` is already
    # provided by modules/apps/packages.nix on darwin.
    home.packages =
      if pkgs.stdenv.isDarwin then
        with pkgs;
        [
          coreutils # readlink --canonicalize, date -d, sha256sum, realpath
          findutils # find -printf, xargs, locate, updatedb
          gnused # sed -i '' (BSD sed -i requires explicit empty arg)
          gnugrep # grep -P (PCRE), grep --include
          gawk # gensub(), match() array capture, asort()
          diffutils # diff -u, cmp, diff3
          gnumake # GNU make
          gnupatch # patch --dry-run, fuzz factor defaults
          gnutar # tar --transform, --exclude, owner=0 quirks
          less # newer less with --inc-search, mouse scroll
        ]
      else
        [ ];
  };
}
