import uvicorn

from .config import HOST, PORT

if __name__ == "__main__":
    print("AI Traded: http://%s:%d  (analysis + paper trading only)" % (HOST, PORT))
    uvicorn.run("backend.main:app", host=HOST, port=PORT, log_level="info")
