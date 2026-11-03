"""账号密码管理 + 首次启动自动初始化。

用户 = 账号（登录名）。密码以 PBKDF2 哈希存 web_users.json，不存明文。
用户元数据（名字/角色/目录名）存 users.json。
"""
import hashlib
import hmac
import json
import secrets

import config

PASSWD_FILE = config.DATA_DIR / "web_users.json"


def load_passwd():
    if PASSWD_FILE.exists():
        try:
            return json.loads(PASSWD_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_passwd(p):
    PASSWD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWD_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()


def set_password(user, pw):
    p = load_passwd()
    salt = secrets.token_hex(16)
    p[user] = {"salt": salt, "pass": _hash(pw, salt)}
    save_passwd(p)


def verify(user, pw):
    rec = load_passwd().get(user)
    if not rec:
        return False
    got = _hash(pw, rec["salt"])
    return hmac.compare_digest(got, rec["pass"])


def remove_user(user):
    p = load_passwd()
    p.pop(user, None)
    save_passwd(p)


def create_user(name, role="user", password=None):
    """建账号 + 建目录 + 写 users.json。返回 (是否新建, 生成的密码或 None)。"""
    users = config.load_users()
    is_new = name not in users
    users[name] = {"dir": name, "role": role}
    config.save_users(users)
    config.ensure_user_dirs(name)
    config.reload_users()
    generated = None
    if is_new or password:
        if not password:
            password = secrets.token_hex(6)
            generated = password
        set_password(name, password)
    return is_new, generated


def list_users():
    users = config.load_users() or config.env_users()
    passwd = load_passwd()
    rows = []
    for name, info in users.items():
        rows.append({"name": name, "role": info.get("role", "user"),
                     "dir": info.get("dir", name),
                     "has_password": name in passwd})
    return rows


def _init_passwords(users):
    """给 .env 声明的用户生成密码：优先 PASS_<name>，否则随机生成并打印。"""
    msgs = []
    for name in users:
        pw = config.ENV.get("PASS_" + name, "").strip()
        generated = ""
        if not pw:
            pw = secrets.token_hex(6)
            generated = pw
        set_password(name, pw)
        if generated:
            msgs.append("  账号 %s / 初始密码 %s" % (name, pw))
    if msgs:
        print("\n".join(["首次启动：已创建以下账号（登录后请修改密码）"] + msgs))


def ensure_bootstrapped():
    """首次运行：确保至少一个账号 + 目录。返回需要打印的说明文字（无则 None）。"""
    if config.load_users():
        return None

    # 1) .env 里配了 USERS
    users = config.env_users()
    if users:
        config.save_users(users)
        for name in users:
            config.ensure_user_dirs(name)
        _init_passwords(users)
        config.reload_users()
        return None

    # 2) 旧数据兜底：web_users.json 里有账号但 users.json 没有（旧版迁移）
    passwd = load_passwd()
    if passwd:
        admin_hint = config.ENV.get("ADMIN_USER", "").strip()
        users = {}
        for name in passwd:
            role = "admin" if (name == admin_hint or name == "admin"
                               or len(users) == 0) else "user"
            users[name] = {"dir": name, "role": role}
        config.save_users(users)
        for name in users:
            config.ensure_user_dirs(name)
        config.reload_users()
        return None

    # 3) 完全空：建默认管理员
    admin = config.ENV.get("ADMIN_USER", "").strip() or "admin"
    users = {admin: {"dir": admin, "role": "admin"}}
    config.save_users(users)
    config.ensure_user_dirs(admin)
    pw = config.ENV.get("ADMIN_PASS", "").strip()
    generated = ""
    if not pw:
        pw = secrets.token_hex(6)
        generated = pw
    set_password(admin, pw)
    config.reload_users()
    if generated:
        return ("首次启动：已创建管理员账号\n"
                "  用户名: %s\n"
                "  密码:   %s\n"
                "  （登录后请立即修改密码）" % (admin, pw))
    return None
