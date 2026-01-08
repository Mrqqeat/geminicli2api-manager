import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# 1. 禁用安全警告（避免控制台全是警告信息）
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 2. 备份原始的 request 方法
old_request = requests.Session.request

# 3. 重写 request 方法，强制设置 verify=False
def new_request(*args, **kwargs):
    kwargs['verify'] = False
    return old_request(*args, **kwargs)

requests.Session.request = new_request


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