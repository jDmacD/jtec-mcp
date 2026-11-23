# Home Manager module: install jtec-mcp and register it with Claude Code.
#
#   imports = [ inputs.jtec-mcp.homeManagerModules.default ];
#   services.jtec-mcp = {
#     enable = true;
#     settings.services.slskd = { url = "https://slskd.example.lan"; key_field = "SLSKD_API_KEY"; };
#     secretspec.provider = "bws://<project-uuid>";
#     secretFiles.BWS_ACCESS_TOKEN = config.sops.secrets.bws_token.path;
#   };
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.jtec-mcp;
  toml = pkgs.formats.toml { };

  configFile = toml.generate "jtec-mcp-config.toml" cfg.settings;

  # Claude Code starts MCP servers with a fixed command and environment, so
  # secrets (the secret provider's own login) are read from files at launch.
  launcher = pkgs.writeShellApplication {
    name = "jtec-mcp-launch";
    text = ''
      ${lib.concatStrings (
        lib.mapAttrsToList (name: file: ''
          ${name}="$(< ${lib.escapeShellArg file})"
          export ${name}
        '') cfg.secretFiles
      )}
      ${lib.concatStrings (
        lib.mapAttrsToList (name: value: ''
          export ${name}=${lib.escapeShellArg value}
        '') cfg.environment
      )}
      export JTEC_MCP_CONFIG=${configFile}
      ${lib.optionalString (cfg.secretspec.provider != null) ''
        export SECRETSPEC_PROVIDER=${lib.escapeShellArg cfg.secretspec.provider}
      ''}
      ${lib.optionalString (cfg.secretspec.file != null) ''
        export JTEC_MCP_SECRETSPEC=${cfg.secretspec.file}
      ''}
      exec ${lib.getExe cfg.package} "$@"
    '';
  };

  services = lib.attrNames (cfg.settings.services or { });

  # Home Manager ships mcpServers in a generated plugin named `hm`, and
  # Claude Code namespaces plugin tools as mcp__plugin_<plugin>_<server>__.
  toolPrefix = "mcp__plugin_hm_${cfg.claudeCode.serverName}__";
in
{
  options.services.jtec-mcp = {
    enable = lib.mkEnableOption "jtec-mcp, an MCP server with workflow-level tools for self-hosted services";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "jtec-mcp.packages.\${system}.default";
      description = "The secretspec-wrapped jtec-mcp package.";
    };

    settings = lib.mkOption {
      type = toml.type;
      default = { };
      example = lib.literalExpression ''
        {
          services.slskd = {
            url = "https://slskd.example.lan";
            key_field = "SLSKD_API_KEY";
          };
        }
      '';
      description = "Contents of jtec-mcp's config.toml (no secrets). See config.example.toml.";
    };

    secretspec = {
      provider = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "bws://00000000-0000-0000-0000-000000000000";
        description = "secretspec provider the API keys are read from (SECRETSPEC_PROVIDER).";
      };
      file = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Your own secretspec.toml, to rename keys (JTEC_MCP_SECRETSPEC). Default: the bundled one.";
      };
    };

    secretFiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = lib.literalExpression ''{ BWS_ACCESS_TOKEN = config.sops.secrets.bws_token.path; }'';
      description = ''
        Environment variables read from files when the server starts, e.g. the
        secret provider's access token. Paths are read at runtime, so sops-nix
        or agenix secret paths work and nothing secret enters the Nix store.
      '';
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra (non-secret) environment variables for the server.";
    };

    claudeCode = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Register the server with Claude Code (programs.claude-code.mcpServers).";
      };
      serverName = lib.mkOption {
        type = lib.types.str;
        default = "jtec";
        description = "MCP server name. Home Manager registers servers through its generated `hm` plugin, so Claude Code names the tools mcp__plugin_hm_<name>__<tool>.";
      };
      context = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Add a short note about the server to Claude Code's global context.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ launcher ];

    programs.claude-code = lib.mkIf cfg.claudeCode.enable {
      mcpServers.${cfg.claudeCode.serverName} = {
        command = lib.getExe launcher;
      };
      context = lib.mkIf cfg.claudeCode.context ''

        ## jtec-mcp

        The `${cfg.claudeCode.serverName}` MCP server (tools named
        `${toolPrefix}<service>_<tool>`) has workflow-level
        tools for: ${if services == [ ] then "no services yet" else lib.concatStringsSep ", " services}.
        Prefer these tools to hand-written API calls (curl, `kubectl exec`,
        port-forwards) for those services. Source and contributor guide:
        https://github.com/jDmacD/jtec-mcp (see its CLAUDE.md).
      '';
    };
  };
}
