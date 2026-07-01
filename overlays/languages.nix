self: super: {
  perlPackages = super.perlPackages // {
    DBDCSV = super.perlPackages.DBDCSV.overrideAttrs (_old: {
      doCheck = false;
    });
  };
}
