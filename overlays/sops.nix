{ super }:

super.sops.overrideAttrs (_oldAttrs: rec {
  version = "3.10.0";
  src = super.fetchFromGitHub {
    owner = "getsops";
    repo = "sops";
    rev = "v${version}";
    hash = "sha256-NOZvVL4b7+TVlB6iM4HJDa5PHOjvcN0BXDMOHmqg7lU=";
  };
  vendorHash = "sha256-I+iwimrNdKABZFP2etZTQJAXKigh+0g/Jhip86Cl5Rg=";
})
