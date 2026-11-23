{
  description = "jtec-mcp: MCP server with workflow-level tools for self-hosted homelab services";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      # Python dependencies come from uv.lock; uv is the source of truth.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        )
      );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          venv = pythonSets.${system}.mkVirtualEnv "jtec-mcp-env" workspace.deps.default;
        in
        rec {
          inherit venv;

          # The server itself only reads environment variables. secretspec
          # resolves them from the configured provider (SECRETSPEC_PROVIDER)
          # before starting it.
          jtec-mcp = pkgs.writeShellApplication {
            name = "jtec-mcp";
            # Provider CLIs (bws, sops, ...) come from the user's PATH;
            # bws is unfree, so the flake doesn't pull it in.
            runtimeInputs = [ pkgs.secretspec ];
            text = ''
              exec secretspec run \
                --file "''${JTEC_MCP_SECRETSPEC:-${./secretspec.toml}}" \
                --reason "''${SECRETSPEC_REASON:-jtec-mcp server}" \
                --caller jtec-mcp \
                -- ${venv}/bin/jtec-mcp-server "$@"
            '';
          };

          default = jtec-mcp;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope (
            lib.composeManyExtensions [
              (workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; })
              (final: prev: {
                jtec-mcp = prev.jtec-mcp.overrideAttrs (old: {
                  nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem { editables = [ ]; };
                });
              })
            ]
          );
          virtualenv = pythonSet.mkVirtualEnv "jtec-mcp-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
              pkgs.secretspec
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );
    };
}
