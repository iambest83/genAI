"""AWS Pricing MCP server — thin wrapper around awslabs.aws-pricing-mcp-server.

Runs the awslabs pricing FastMCP instance over Streamable HTTP on the
AgentCore Runtime's expected port (8000, path /mcp). AgentCore Runtime
with `--protocol MCP` handles the container-level auth (Cognito bearer
token or IAM) and session lifecycle; this file only needs to expose the
FastMCP server on the right socket.

Deployed as a separate AgentCore Runtime (not the main agent runtime).
Registered on `mfmodagent-gateway-<GATEWAY_ID>` as target `AwsPricingMCP`
(mcpServer type). The main agent's mcp_node dispatches pricing tool
calls to this runtime via the Gateway — same code path as MainframeMCP
and AWSMCP.

Tools exposed (from awslabs upstream, all `awslabs___`-prefixed once
registered on Gateway):
  - analyze_cdk_project
  - analyze_terraform_project
  - get_pricing              ← the workhorse for TCO / cost questions
  - get_bedrock_patterns
  - generate_cost_report
  - get_pricing_service_codes
  - get_pricing_service_attributes
  - get_pricing_attribute_values
  - get_price_list_urls

Runtime execution role needs (attach as inline policy):
  pricing:DescribeServices, pricing:GetProducts,
  pricing:GetAttributeValues, pricing:GetPriceListFileUrl
"""
from __future__ import annotations

# Import the awslabs FastMCP instance. The package installs a `mcp` object
# at `awslabs.aws_pricing_mcp_server.server` — same convention as the docs
# server. Reusing their FastMCP means we automatically get every tool they
# ship AND every fix they push upstream on their next release.
from awslabs.aws_pricing_mcp_server.server import mcp


# Override FastMCP transport config for AgentCore Runtime hosting:
#   - host="0.0.0.0"  — bind to all interfaces (Runtime container networking).
#   - stateless_http=True  — pricing calls are one-shot RPC; no multi-turn
#     elicitation needed. Runtime docs recommend stateless for basic tools.
#   - Disable transport-security Host-header check. FastMCP's default rejects
#     any Host header that isn't localhost, which breaks Runtime's internal
#     routing (proxied through `cell*.us-east-1.prod.arp.kepler-analytics.aws.dev`
#     hostnames per Runtime logs). AgentCore Runtime already provides its own
#     network isolation; FastMCP-level Host validation is redundant here.
mcp.settings.host = "0.0.0.0"
mcp.settings.stateless_http = True
if hasattr(mcp.settings, "transport_security"):
    # Newer mcp package: settings.transport_security controls DNS-rebinding
    # protection. AgentCore Runtime routes traffic through internal cell
    # hostnames (cell*.us-east-1.prod.arp.kepler-analytics.aws.dev) which
    # FastMCP rejects with 421 Misdirected Request by default. Disable
    # DNS-rebinding protection entirely — Runtime's own network isolation
    # is the actual security boundary. Also broaden allowed_hosts as a
    # belt-and-suspenders for older mcp package versions that check both.
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.transport_security.allowed_hosts = ["*"]
    mcp.settings.transport_security.allowed_origins = ["*"]


if __name__ == "__main__":
    # AgentCore Runtime with protocol=MCP expects the server at 0.0.0.0:8000/mcp.
    # `streamable-http` is the transport AgentCore proxies through for MCP
    # runtimes.
    mcp.run(transport="streamable-http")
