from judicial_ai.app import create_app

app = create_app(modules=["health", "mcp"])
