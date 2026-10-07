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

  # The JS test harness needs node, but only to check the pure web-UI helpers
  # (node:test, no npm dependencies). It never reaches the packaged output.
  checkInputs = lib.optionals doCheck [ pytest pkgs.nodejs ];
  checkPhase = lib.optionalString doCheck ''
    python -m pytest tests -q
    ${pkgs.nodejs}/bin/node --test tests/js/*.test.js
  '';

  meta = {
    description = "Yet Another Simple Kanban board (yask)";
    mainProgram = "yask";
    platforms = lib.platforms.linux;
  };
}
