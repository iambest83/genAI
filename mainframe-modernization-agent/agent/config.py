"""Agent configuration - models and settings configured via environment variables.

Models:
  BEDROCK_ROUTER_MODEL_ID  — fast/cheap model for routing ambiguous queries (default: Haiku)
  BEDROCK_RESPONSE_MODEL_ID — quality model for responses (default: Sonnet 4.6)
"""
import os
from langchain_aws import ChatBedrock

DEFAULT_ROUTER_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_RESPONSE_MODEL = "us.anthropic.claude-sonnet-4-6"


def get_router_llm():
    """LLM for routing ambiguous queries. Fast and cheap."""
    return ChatBedrock(
        model_id=os.environ.get("BEDROCK_ROUTER_MODEL_ID", DEFAULT_ROUTER_MODEL),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        model_kwargs={
            "temperature": 0.0,
            "max_tokens": 512,
        }
    )


def get_response_llm():
    """LLM for generating final responses."""
    return ChatBedrock(
        model_id=os.environ.get("BEDROCK_RESPONSE_MODEL_ID", DEFAULT_RESPONSE_MODEL),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        model_kwargs={
            "temperature": float(os.environ.get("MODEL_TEMPERATURE", "0.3")),
            "max_tokens": int(os.environ.get("MODEL_MAX_TOKENS", "4096")),
        }
    )


def get_kb_config() -> dict:
    """Return Knowledge Base configuration from environment.

    Retrieval is a two-stage pipeline when rerank_enabled is true:
        Stage 1 (vector ANN): pull `num_results` candidates from OpenSearch
        Stage 2 (cross-encoder rerank): keep top `rerank_num_results`

    `num_results` is the *vector-stage* candidate pool. Default 20 — rerank
    is only useful with a wide candidate set; 5 is too few to leave room
    for the reranker to surface anything the vector stage mis-ranked.

    `rerank_num_results` is the final top-K passed to the response model.
    Default 5 — same headroom math as before.

    `rerank_model_arn` defaults to Cohere Rerank 3.5. Bedrock KB's retrieve()
    accepts this as a BEDROCK_RERANKING_MODEL inside the vectorSearch config.

    `score_floor` is applied to the *post-rerank* relevance score (when
    rerank is enabled) or the raw vector cosine (when disabled). Cohere
    rerank scores are on a different scale than Titan cosine — empirically
    relevant chunks land at 0.5+, clear noise at 0.1-. 0.40 stays as a
    middle-ground default; tune via KB_SCORE_FLOOR.
    """
    return {
        "kb_id": os.environ.get("KB_ID", ""),
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "num_results": int(os.environ.get("KB_NUM_RESULTS", "20")),
        "score_floor": float(os.environ.get("KB_SCORE_FLOOR", "0.40")),
        "rerank_enabled": os.environ.get("KB_RERANK_ENABLED", "true").lower() == "true",
        "rerank_model_arn": os.environ.get(
            "KB_RERANK_MODEL_ARN",
            "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0",
        ),
        "rerank_num_results": int(os.environ.get("KB_RERANK_NUM_RESULTS", "5")),
    }


def get_gateway_config() -> dict:
    """Return AgentCore Gateway configuration from environment."""
    return {
        "gateway_url": os.environ.get("GATEWAY_URL",
            "https://mfmodagent-gateway-<GATEWAY_ID>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"),
        "region": os.environ.get("AWS_REGION", "us-east-1"),
    }
