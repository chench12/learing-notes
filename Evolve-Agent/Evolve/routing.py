"""冻结模型的可重放加权路由；客户端延迟创建，密钥不进入记录。"""

import hashlib
import random
import threading


def select_model(config, index, purpose="program"):
    routes = config["routing"]["models"]
    seed = hashlib.sha256(f"router-v1:{config['search']['seed']}:{purpose}:{index}".encode()).hexdigest()
    if not routes:
        return dict(config["model"]), {"id": "default", "seed_sha256": seed}
    route = random.Random(seed).choices(routes, weights=[item["weight"] for item in routes], k=1)[0]
    return {key: route[key] for key in ("base_url", "model", "key_env", "max_tokens")}, {"id": route["id"], "seed_sha256": seed}


class ModelPool:
    """只在预算检查和计划保存后真正调用所选模型。"""
    def __init__(self):
        self.clients = {}
        self._client_lock = threading.Lock()

    def generate_for(self, config, **kwargs):
        from Evolve.llm import ChatModel
        key = (config["base_url"], config["model"], config["key_env"])
        with self._client_lock:
            if key not in self.clients:
                self.clients[key] = ChatModel(base_url=key[0], model=key[1], key_env=key[2])
            client = self.clients[key]
        return client.generate(**kwargs)


def invoke(model, config, **kwargs):
    if hasattr(model, "generate_for"):
        return model.generate_for(config, **kwargs)
    # 离线夹具/已有单模型接口兼容；真实多模型入口统一使用 ModelPool。
    return model.generate(**kwargs)
