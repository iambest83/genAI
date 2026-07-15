"""Main entry point for the Mainframe Modernization Chat Agent."""
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from .graph import build_graph

load_dotenv()


def run_interactive():
    """Run the agent in interactive CLI mode."""
    graph = build_graph()

    print("=" * 60)
    print("  Mainframe Modernization Chat Agent")
    print("  Financial Services Edition")
    print("=" * 60)
    print()
    print("Type 'quit' to exit.")
    print("=" * 60)
    print()

    state = {
        "messages": [],
        "route": "both",
        "mcp_tools": [],
        "kb_context": "",
        "mcp_context": "",
        "user_query": "",
    }

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        state["messages"].append(HumanMessage(content=user_input))

        print("\nThinking...\n")
        result = graph.invoke(state)

        state = result
        last_msg = state["messages"][-1]
        print(f"{last_msg.content}\n")


if __name__ == "__main__":
    run_interactive()
