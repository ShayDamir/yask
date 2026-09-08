{
  lib,
  pkgs,
  version,
  src,
  doCheck ? false,
}:
let
  pytest = pkgs.python3Packages.pytest;
in
pkgs.python3Packages.buildPythonApplication {
  pname = "yask";
  inherit version src;
  pyproject = true;

  nativeBuildInputs = [ pkgs.python3Packages.setuptools ];

  dependencies = [
    pkgs.python3Packages.fastapi
    pkgs.python3Packages.uvicorn
    pkgs.python3Packages.mcp
    pkgs.python3Packages.httpx
    pkgs.python3Packages.pydantic
  ];

  # nix develop (without a devShell) inherits the package's buildInputs.
  buildInputs = [
    pkgs.python3Packages.fastapi
    pkgs.python3Packages.uvicorn
    pkgs.python3Packages.mcp
    pkgs.python3Packages.httpx
    pkgs.python3Packages.pydantic
    pytest
  ];

  checkInputs = lib.optionals doCheck [ pytest ];
  checkPhase = lib.optionalString doCheck "python -m pytest tests -q";

  meta = {
    description = "Yet Another Simple Kanban board (yask)";
    mainProgram = "yask";
    platforms = lib.platforms.linux;
  };
}
