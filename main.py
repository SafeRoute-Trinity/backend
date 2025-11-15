# 可选：backend/main.py
# uvicorn main:app --host 0.0.0.0 --port 20009 --reload
from fastapi import FastAPI

app = FastAPI(title="Service Index", version="1.0.0")


@app.get("/")
async def index():
    return {
        "services": {
            "user_management": "http://127.0.0.1:20000/docs",
            "notification": "http://127.0.0.1:20001/docs",
            "routing_service": "http://127.0.0.1:20002/docs",
            "safety_scoring": "http://127.0.0.1:20003/docs",
            "feedback": "http://127.0.0.1:20004/docs",
            "data_cleaner": "http://127.0.0.1:20005/docs",
            "sos": "http://127.0.0.1:20006/docs",
        }
    }
