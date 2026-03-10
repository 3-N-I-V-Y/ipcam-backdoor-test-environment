from fastapi import FastAPI, Request
from typing import Any

app = FastAPI()

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.post("/beacon")
async def beacon(request: Request) -> dict[str, Any]:
    payload = await request.json()
    return {"received": True, "payload": payload}

@app.get("/task")
def task(camera_id: str | None = None) -> dict[str, Any]:
    return {
        "camera_id": camera_id,
        "task": {
            "command": "noop",
            "params": {}
        }
    }

@app.post("/result")
async def result(request: Request) -> dict[str, Any]:
    payload = await request.json()
    return {"saved": True, "payload": payload}