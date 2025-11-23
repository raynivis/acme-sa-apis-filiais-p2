import requests
from datetime import datetime
from typing import List, Dict
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os

def load_replicas(exclude_api: str):
    replicas = {}
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    all_apis = ['matriz', 'alipio', 'laranjeiras', 'alvorada']
    all_apis.remove(exclude_api)
    
    for api_name in all_apis:
        env_path = os.path.join(base_path, api_name, '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    if line.startswith('API_PORT='):
                        port = line.strip().split('=')[1]
                        replicas[api_name] = f"http://localhost:{port}"
                        break
    return replicas

class ReplicaManager:
    def __init__(self, current_api_name: str, replicas: Dict[str, str]):
        self.current_api_name = current_api_name
        self.replicas = replicas
        self.timeout = 5.0
        self.executor = ThreadPoolExecutor(max_workers=10)
    
    def _check_replica_health_sync(self, name: str, url: str) -> Dict:
        try:
            start_time = datetime.now()
            response = requests.get(f"{url}/status", timeout=self.timeout)
            latency = (datetime.now() - start_time).total_seconds() * 1000
            
            if response.status_code == 401:
                return {
                    "nome": name,
                    "url": url,
                    "status": "online",
                    "latencia_ms": round(latency, 2)
                }
            else:
                return {
                    "nome": name,
                    "url": url,
                    "status": "erro",
                    "latencia_ms": None
                }
        except Exception as e:
            return {
                "nome": name,
                "url": url,
                "status": "offline",
                "latencia_ms": None,
                "erro": str(e)
            }
    
    async def check_all_replicas(self) -> List[Dict]:
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                self.executor,
                self._check_replica_health_sync,
                name,
                url
            )
            for name, url in self.replicas.items()
        ]
        results = await asyncio.gather(*tasks)
        return results
