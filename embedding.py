import httpx

from config import ENV

# 单次请求最多带多少条文本。硅基流动/智谱对 batch 都有上限，
# 而调用方（rag / persona）可能一次塞几十条，这里统一兜住。
MAX_BATCH = 16


class Embedder:
    """把文本向量化，供语义检索使用。

    配置优先级：EMBED_*（独立向量服务）> GLM_*（复用智谱 key）。

    踩过的坑：.env 里早就配了 EMBED_API_KEY / EMBED_BASE_URL，但代码只读 GLM_*，
    导致换向量供应商时改了配置却不生效（表现为一直报旧供应商的错）。现在统一
    EMBED_* 优先，换家只改 .env，不动代码。
    """

    def __init__(self):
        self.key = (ENV.get("EMBED_API_KEY") or ENV.get("GLM_API_KEY") or "").strip()
        base = (ENV.get("EMBED_BASE_URL") or ENV.get("GLM_BASE_URL")
                or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        self.url = base + "/embeddings"
        self.model = ENV.get("EMBED_MODEL", "embedding-2")
        self.dim = self._dim()
        self.client = httpx.Client(timeout=60)

    def _dim(self):
        """EMBED_DIM 显式指定优先，否则按模型名推断。

        注意：不同模型的向量空间不通用，换模型必须重建索引。
        """
        d = str(ENV.get("EMBED_DIM", "")).strip()
        if d.isdigit():
            return int(d)
        if "embedding-3" in self.model:
            return 2048
        return 1024          # embedding-2 / BAAI/bge-m3 / bge-large-zh 均为 1024

    def _post(self, texts):
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

    def embed(self, texts):
        """输入字符串或字符串列表，返回对应的向量列表（顺序与输入一致）。"""
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []
        if len(texts) <= MAX_BATCH:
            return self._post(texts)
        out = []
        for i in range(0, len(texts), MAX_BATCH):
            out.extend(self._post(texts[i:i + MAX_BATCH]))
        return out
