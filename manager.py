import os
import json
import time
import sys
import subprocess
import requests
import uvicorn
import asyncio
from typing import List, Dict, Optional
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from src.config import CLIENT_ID, CLIENT_SECRET

# --- 配置 ---
MANAGEMENT_PORT = 3000
TOKENS_DIR = os.path.join(os.getcwd(), "tokens")
CONFIG_FILE = "servers_config.json"

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 进程存储: { port: subprocess.Popen }
running_processes: Dict[int, subprocess.Popen] = {}

# --- 数据模型 ---
class ServerConfig(BaseModel):
    id: Optional[str] = None
    name: str
    token_file: str
    project_id: str             # 当前选中的 ID
    project_ids: List[str] = [] # ID 列表历史
    port: int
    password: str
    is_pro: bool = False
    status: str = "stopped"

# --- 核心逻辑 ---
def load_config() -> List[Dict]:
    if not os.path.exists(CONFIG_FILE): return []
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_config(configs: List[Dict]):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(configs, f, indent=2)

def get_token_files():
    if not os.path.exists(TOKENS_DIR): os.makedirs(TOKENS_DIR)
    return [f for f in os.listdir(TOKENS_DIR) if f.endswith('.json')]

def fetch_account_data_sync(filename, project_id):
    """同步获取单个账号额度"""
    file_path = os.path.join(TOKENS_DIR, filename)
    try:
        if not os.path.exists(file_path): raise Exception(f"文件 {filename} 不存在")
        
        with open(file_path, 'r') as f: token_data = json.load(f)
        
        # 补全 Client 信息
        for k, v in {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "token_uri": "https://oauth2.googleapis.com/token"}.items():
            if k not in token_data: token_data[k] = v
            
        # 构造凭证，添加了 profile 和 openid 权限以获取昵称和头像
        SCOPES = [
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "openid"
        ]
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                token_data.update(json.loads(creds.to_json()))
                with open(file_path, 'w') as f: json.dump(token_data, f, indent=2)
            except Exception as e: print(f"[{filename}] Refresh failed: {e}")

        headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json", "User-Agent": "GeminiCLI/v0.1.5"}
        
        # 并发请求 UserInfo 和 Quota
        with requests.Session() as s:
            s.headers.update(headers)
            user_resp = s.get("https://www.googleapis.com/oauth2/v2/userinfo", timeout=8)
            quota_resp = s.post("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota", json={"project": project_id}, timeout=8)

        if quota_resp.status_code != 200: raise Exception(f"API Error {quota_resp.status_code}")
            
        return {
            "status": "success", 
            "filename": filename, 
            "user": user_resp.json(), 
            "quotas": quota_resp.json().get("buckets", [])
        }
    except Exception as e:
        return {"status": "error", "filename": filename, "message": str(e)}

# --- 路由 ---
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/api/tokens")
async def list_tokens():
    return get_token_files()

@app.get("/api/servers")
async def get_servers():
    configs = load_config()
    for cfg in configs:
        port = cfg['port']
        # 检查进程存活状态
        if port in running_processes:
            if running_processes[port].poll() is None:
                cfg['status'] = "running"
            else:
                del running_processes[port]
                cfg['status'] = "stopped"
        else:
            cfg['status'] = "stopped"
    return configs

@app.post("/api/servers")
async def add_server(config: ServerConfig):
    configs = load_config()
    config.id = str(int(time.time() * 1000))
    data = config.dict()
    del data['status']
    # 确保 project_id 在列表中
    if data['project_id'] not in data['project_ids']:
        data['project_ids'].append(data['project_id'])
    configs.append(data)
    save_config(configs)
    return {"status": "success"}

@app.put("/api/servers/{server_id}")
async def update_server(server_id: str, config: ServerConfig):
    configs = load_config()
    for i, cfg in enumerate(configs):
        if cfg['id'] == server_id:
            updated_data = config.dict()
            updated_data['id'] = server_id
            updated_data['status'] = cfg.get('status', 'stopped')
            if updated_data['project_id'] not in updated_data['project_ids']:
                updated_data['project_ids'].append(updated_data['project_id'])
            configs[i] = updated_data
            save_config(configs)
            return {"status": "success"}
    return JSONResponse(status_code=404, content={"message": "Not found"})

@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str):
    configs = load_config()
    target = next((c for c in configs if c['id'] == server_id), None)
    if target and target['port'] in running_processes:
        await stop_server(server_id)
    save_config([c for c in configs if c['id'] != server_id])
    return {"status": "success"}

@app.post("/api/servers/reorder")
async def reorder_servers(order: List[str]):
    configs = load_config()
    config_dict = {c['id']: c for c in configs}
    new_configs = [config_dict[sid] for sid in order if sid in config_dict]
    # 追加未在排序列表中的新配置
    existing_ids = set(order)
    new_configs.extend([c for c in configs if c['id'] not in existing_ids])
    save_config(new_configs)
    return {"status": "success"}

# --- 进程管理 ---
@app.post("/api/servers/{server_id}/start")
async def start_server(server_id: str):
    configs = load_config()
    target = next((c for c in configs if c['id'] == server_id), None)
    if not target: return JSONResponse(404, {"message": "Server not found"})
    
    port = target['port']
    if port in running_processes and running_processes[port].poll() is None:
        return {"status": "already_running"}

    env = os.environ.copy()
    env.update({
        "GOOGLE_APPLICATION_CREDENTIALS": os.path.join(TOKENS_DIR, target['token_file']),
        "GOOGLE_CLOUD_PROJECT": target['project_id'],
        "HOST": "0.0.0.0",
        "PORT": str(port),
        "GEMINI_AUTH_PASSWORD": target['password']
    })

    proc = subprocess.Popen([sys.executable, "run_proxy.py"], env=env, cwd=os.getcwd())
    running_processes[port] = proc
    return {"status": "started", "pid": proc.pid}

@app.post("/api/servers/{server_id}/stop")
async def stop_server(server_id: str):
    configs = load_config()
    target = next((c for c in configs if c['id'] == server_id), None)
    if not target: return JSONResponse(404, {"message": "Server not found"})
    
    port = target['port']
    if port in running_processes:
        proc = running_processes[port]
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except:
            proc.kill()
        del running_processes[port]
        return {"status": "stopped"}
    return {"status": "not_running"}

# --- 额度查询 (优化版: 单个查询) ---
@app.get("/api/servers/{server_id}/quota")
async def get_server_quota(server_id: str):
    configs = load_config()
    target = next((c for c in configs if c['id'] == server_id), None)
    if not target: return JSONResponse(404, {"message": "Server not found"})

    loop = asyncio.get_running_loop()
    # 在线程池中执行耗时操作
    res = await loop.run_in_executor(None, fetch_account_data_sync, target['token_file'], target['project_id'])
    
    res['config_name'] = target['name']
    res['is_pro'] = target.get('is_pro', False)
    return res

if __name__ == "__main__":
    print(f"Manage Server running at http://localhost:{MANAGEMENT_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=MANAGEMENT_PORT)