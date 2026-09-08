{
  description = "yask — Yet Another Simple Kanban board";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = {
    self,
    nixpkgs,
    ...
  }:
  let
    inherit (nixpkgs) lib;
    eachSystem = lib.genAttrs lib.systems.flakeExposed;
    version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
    pkgsFor =
      eachSystem
      (system:
        import nixpkgs {
          localSystem.system = system;
        });
  in
  {
    packages =
      eachSystem
      (system:
        {
          default =
            pkgsFor.${system}.callPackage ./package.nix {
              inherit version;
              src = lib.cleanSource ./.;
            };
        });

    checks =
      eachSystem
      (system:
        {
          default =
            pkgsFor.${system}.callPackage ./package.nix {
              inherit version;
              src = lib.cleanSource ./.;
              doCheck = true;
            };
        });
  };
}
