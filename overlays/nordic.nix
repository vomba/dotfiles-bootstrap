# Restored from nixpkgs (removed 2026-07-22 with gtk-engine-murrine / GTK 2).
# GTK 3/4 CSS themes do not need the Murrine engine; GTK 2 theme files are dropped.
{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
  jdupes,
}:

stdenvNoCC.mkDerivation {
  pname = "nordic";
  version = "2.2.0";

  srcs = [
    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "v2.2.0";
      hash = "sha256-wTWHdao/1RLqUmqh/9gEyhERGymFWHqiC97JD28LSgk=";
      name = "Nordic";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "e1ee76f47ebf06ecbee41c95a80a61a739e22785";
      hash = "sha256-A6PUrIw/XWqADg/MmfVic2nY0/3vlzSnqDK2Tre5eAA=";
      name = "Nordic-standard-buttons";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "5122374e9112ccc803f4b8702263f7f099fe525d";
      hash = "sha256-Vr+zRSWQzZxBkG6fJJyVZzl3Qy2Cka9JRQa3QkA+YZA=";
      name = "Nordic-darker";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "d4cec0c58d19bfa9e6395b9dff37b770a3e4f9ee";
      hash = "sha256-9i1yEQlpB+sWZpPz1gNGTfnRGU+j4l0VScRJh2qsUMM=";
      name = "Nordic-darker-standard-buttons";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "e10f44d8ba698677bfda79aa5ebfd3117a885e1d";
      hash = "sha256-fa8XLUoLoahYlAcwYVjrOOU03JbwbbSfrwmgSDzHk6w=";
      name = "Nordic-bluish-accent";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic";
      rev = "be6abf568801f84b07dccafbd61c1e9d070191cd";
      hash = "sha256-EI9FqkHfABfWM8bP5duwgNmD839KOKhcIAfkkfGhZ/E=";
      name = "Nordic-bluish-accent-standard-buttons";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic-polar";
      rev = "4103a3d4e3445e71de6544b348a3562fa2021ead";
      hash = "sha256-fjTc/KiDdfdbcSw3CRuL61tsdvf1jCae5wr2prA29fA=";
      name = "Nordic-Polar";
    })

    (fetchFromGitHub {
      owner = "EliverLara";
      repo = "nordic-polar";
      rev = "31319bcd1c51362c98fec67e7ffff7af1544b2c8";
      hash = "sha256-q5JClikEFaurHzHbNiTORnR9sg8cC6jW0Wk6OJ1RS1g=";
      name = "Nordic-Polar-standard-buttons";
    })
  ];

  sourceRoot = ".";

  nativeBuildInputs = [ jdupes ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/themes
    cp -a Nordic* $out/share/themes

    rm -r $out/share/themes/*/.gitignore
    rm -r $out/share/themes/*/Art
    rm -r $out/share/themes/*/FUNDING.yml
    rm -r $out/share/themes/*/README.md
    rm -r $out/share/themes/*/{package.json,package-lock.json,Gulpfile.js}
    rm -r $out/share/themes/*/src
    rm -r $out/share/themes/*/cinnamon/*.scss
    rm -r $out/share/themes/*/gnome-shell/{earlier-versions,extensions,*.scss}
    # GTK 2 required gtk-engine-murrine (removed from nixpkgs); drop those trees.
    rm -rf $out/share/themes/*/gtk-2.0
    rm -r $out/share/themes/*/gtk-3.0/{apps,widgets,*.scss}
    rm -r $out/share/themes/*/gtk-4.0/{apps,widgets,*.scss}
    rm -r $out/share/themes/*/xfwm4/{assets,render_assets.fish}

    mkdir -p $out/share/wallpapers/Nordic
    mv -v $out/share/themes/Nordic/extras/wallpapers/* $out/share/wallpapers/Nordic/
    rmdir $out/share/themes/Nordic/extras{/wallpapers,}

    mkdir -p $out/share/icons
    mv -v $out/share/themes/Nordic/kde/folders/* $out/share/icons/
    rm -rf $out/share/themes/Nordic/kde

    jdupes --quiet --link-soft --recurse $out/share

    # FIXME: https://github.com/EliverLara/Nordic/issues/331
    echo "Removing broken symlinks ..."
    find $out -xtype l -print -delete

    runHook postInstall
  '';

  meta = {
    description = "Gtk themes using the Nord color palette (GTK 3/4 only; vendored after nixpkgs removal)";
    homepage = "https://github.com/EliverLara/Nordic";
    license = lib.licenses.gpl3Only;
    platforms = lib.platforms.all;
    maintainers = with lib.maintainers; [ romildo ];
  };
}
