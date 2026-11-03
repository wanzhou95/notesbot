import httpx
from config import ENV


class Embedder:
    """用智谱 embedding 接口（复用 GLM key），把文本向量化。"""

    def __init__(self):
        self.key = ENV.get("GLM_API_KEY", "")
        base = ENV.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        self.url = base + "/embeddings"
        self.model = ENV.get("EMBED_MODEL", "embedding-2")
        # embedding-2 维度 1024；embedding-3 维度 2048
        self.dim = 2048 if "embedding-3" in self.model else 1024
        self.client = httpx.Client(timeout=60)

    def embed(self, texts):
        """输入字符串或字符串列表，返回对应的向量列表。"""
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []
        r = self.client.post(
            self.url,
            headers={"Authorization": "Bearer " + self.key},
            json={"model": self.model, "input": texts},
        )
        if r.status_code != 200:
            raise RuntimeError("embedding HTTP %s %s" % (r.status_code, r.text[:120]))
        data = r.json()
        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        return [it.get("embedding", []) for it in items]
