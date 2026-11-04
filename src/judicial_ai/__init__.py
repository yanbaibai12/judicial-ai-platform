"""司法 AI 中台核心包。"""

from .client import AsyncClient, Client, JudicialAIError

__version__ = "0.2.0"

__all__ = ["AsyncClient", "Client", "JudicialAIError", "__version__"]
