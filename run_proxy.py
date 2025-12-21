import os
import uvicorn
import sys

sys.path.append(os.getcwd())

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    
    from src.main import app
    
    print(f"Starting Proxy on port {port}...")
    uvicorn.run(app, host=host, port=port)