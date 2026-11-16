"""Test-session guard: no test may reach a local or hosted model.

Embedding, rerank and generation calls are replayed from committed caches. Any cache miss must fail loudly
instead of silently running inference, so the endpoint is pointed at a closed port and the evaluation
package's inference switch is turned on before any test module is imported.
"""
import os

os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["EVALS_OLLAMA_URL"] = "http://127.0.0.1:9"

from evals import ollama  # noqa: E402

ollama.INFERENCE_BLOCKED = True
