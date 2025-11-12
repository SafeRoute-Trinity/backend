from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def data_cleaner():
    return {"message": "SOS service"}
