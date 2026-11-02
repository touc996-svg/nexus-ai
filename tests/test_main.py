from fastapi import FastAPI

app = FastAPI(
    title="NEXUS AI",
    description="Unified Enterprise, Personal Assistant, and Cybersecurity AI Platform",
    version="0.1.0"
)

@app.get("/")
def home():
    return {
        "message": "NEXUS AI is online!",
        "status": "operational"
    }