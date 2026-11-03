from openai import OpenAI

OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "qwen3:1.7b"

client = OpenAI(
    base_url=OLLAMA_BASE_URL,
    api_key="ollama",
    timeout=120.0,
)
