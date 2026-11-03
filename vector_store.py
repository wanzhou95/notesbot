import json
import sqlite3

import sqlite_vec


class VectorStore:
    """基于 sqlite-vec 的向量库：chunks 存向量，chunk_meta 存原文与归属。"""

    def __init__(self, db_path, dim=1024):
        self.dim = dim
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(embedding float[%d])" % dim
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS chunk_meta ("
            "rowid INTEGER PRIMARY KEY, user TEXT, path TEXT, text TEXT)"
        )
        self.conn.commit()

    def clear_user(self, user):
        rows = self.conn.execute(
            "SELECT rowid FROM chunk_meta WHERE user=?", (user,)
        ).fetchall()
        for (rid,) in rows:
            self.conn.execute("DELETE FROM chunks WHERE rowid=?", (rid,))
            self.conn.execute("DELETE FROM chunk_meta WHERE rowid=?", (rid,))
        self.conn.commit()

    def delete_file(self, user, path):
        rows = self.conn.execute(
            "SELECT rowid FROM chunk_meta WHERE user=? AND path=?", (user, path)
        ).fetchall()
        for (rid,) in rows:
            self.conn.execute("DELETE FROM chunks WHERE rowid=?", (rid,))
            self.conn.execute("DELETE FROM chunk_meta WHERE rowid=?", (rid,))
        self.conn.commit()

    def add(self, user, path, text, emb):
        emb_json = json.dumps(emb)
        cur = self.conn.execute(
            "INSERT INTO chunk_meta(user, path, text) VALUES (?,?,?)", (user, path, text)
        )
        rowid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO chunks(rowid, embedding) VALUES (?,?)", (rowid, emb_json)
        )
        self.conn.commit()
        return rowid

    def search(self, user, query_emb, limit=5):
        emb_json = json.dumps(query_emb)
        rows = self.conn.execute(
            "SELECT rowid, distance FROM chunks WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (emb_json, max(limit * 8, 60)),
        ).fetchall()
        results = []
        for rowid, dist in rows:
            meta = self.conn.execute(
                "SELECT path, text FROM chunk_meta WHERE rowid=? AND user=?",
                (rowid, user),
            ).fetchone()
            if meta:
                results.append({"path": meta[0], "text": meta[1], "distance": dist})
            if len(results) >= limit:
                break
        return results
