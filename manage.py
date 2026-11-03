#!/usr/bin/env python3
"""NotesBot 命令行管理工具。

用法示例：
  python manage.py init                       # 初始化：按 .env 的 USERS 建账号和目录
  python manage.py adduser bob --role user    # 新增账号（交互式设密码，回车自动生成随机密码）
  python manage.py adduser bob --pass 123456  # 新增账号并指定密码
  python manage.py passwd bob                 # 改密码（交互式输入）
  python manage.py list                       # 列出所有用户
  python manage.py reindex [name]             # 重建语义索引（name 留空则全部重建）
"""
import argparse
import getpass
import sys

import config
import users


def cmd_init(args):
    config.reload_users()
    msg = users.ensure_bootstrapped()
    if msg:
        print(msg)
        return
    rows = users.list_users()
    if not rows:
        print("没有用户。请在 .env 配置 USERS=名字:角色,... 后重试，或用 adduser。")
        return
    print("已初始化，现有用户：")
    for r in rows:
        print("  - %s (role=%s, dir=%s)" % (r["name"], r["role"], r["dir"]))


def cmd_adduser(args):
    name = args.name.strip()
    if not name or any(c in name for c in " ,:/\\"):
        print("用户名不能为空，且不能含空格/逗号/冒号/斜杠")
        sys.exit(1)
    pw = args.password
    if not pw:
        pw = getpass.getpass("为该账号设置密码（回车则自动生成随机密码）：") or None
    is_new, generated = users.create_user(name, role=args.role, password=pw)
    if generated:
        print("已创建账号 %s（角色 %s），初始密码：%s" % (name, args.role, generated))
    else:
        print("账号 %s 已创建/更新（角色 %s）" % (name, args.role))
    print("提示：若服务正在运行，新账号首次登录时自动生效，无需重启。")


def cmd_passwd(args):
    name = args.name.strip()
    if name not in config.load_users():
        print("账号 %s 不存在" % name)
        sys.exit(1)
    pw = getpass.getpass("新密码：")
    pw2 = getpass.getpass("再输一次：")
    if pw != pw2:
        print("两次输入不一致")
        sys.exit(1)
    if len(pw) < 4:
        print("密码太短（至少 4 位）")
        sys.exit(1)
    users.set_password(name, pw)
    print("密码已更新")


def cmd_list(args):
    rows = users.list_users()
    if not rows:
        print("（无用户）")
        return
    for r in rows:
        mark = "有密码" if r["has_password"] else "⚠️ 无密码"
        print("  %-16s role=%-5s dir=%-16s %s" % (r["name"], r["role"], r["dir"], mark))


def cmd_reindex(args):
    import rag
    if args.name:
        if args.name not in config.load_users():
            print("账号 %s 不存在" % args.name)
            sys.exit(1)
        n = rag.reindex_user(args.name)
        print("已重建 %s 的索引：%d 个片段" % (args.name, n))
    else:
        for r in users.list_users():
            n = rag.reindex_user(r["name"])
            print("已重建 %s 的索引：%d 个片段" % (r["name"], n))


def main():
    p = argparse.ArgumentParser(description="NotesBot 管理工具")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="初始化账号和目录")

    a = sub.add_parser("adduser", help="新增账号")
    a.add_argument("name")
    a.add_argument("--role", default="user", choices=["admin", "user"])
    a.add_argument("--pass", dest="password", default=None,
                   help="初始密码（缺省交互输入或随机生成）")

    b = sub.add_parser("passwd", help="改密码")
    b.add_argument("name")

    sub.add_parser("list", help="列出用户")

    r = sub.add_parser("reindex", help="重建语义索引")
    r.add_argument("name", nargs="?", default=None)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(0)
    {"init": cmd_init, "adduser": cmd_adduser, "passwd": cmd_passwd,
     "list": cmd_list, "reindex": cmd_reindex}[args.cmd](args)


if __name__ == "__main__":
    main()
