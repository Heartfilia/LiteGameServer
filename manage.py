#!/usr/bin/env python3
"""Local administration only. No user-provisioning HTTP endpoint exists."""
import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from litegame_server.config import Settings
from litegame_server.db import SCHEMA_VERSION, connect, initialize
from litegame_server.security import hash_password, normalize_username, utcnow


def read_password():
    if not sys.stdin.isatty():
        raise ValueError("请在交互终端输入密码；不接受命令参数或管道中的明文密码")
    password = getpass.getpass("密码（至少 4 个字符）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise ValueError("两次密码不一致")
    return hash_password(password)


def create_user(conn, username, password_hash):
    display, key = normalize_username(username)
    conn.execute("INSERT INTO users(username,username_key,password_hash,created_at) VALUES(?,?,?,?)",
                 (display, key, password_hash, utcnow()))


def update_user(conn, username, action, password_hash=None):
    _, key = normalize_username(username)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id FROM users WHERE username_key=?", (key,)).fetchone()
    if not row:
        raise ValueError("用户不存在")
    if action == "reset-password":
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, row["id"]))
    elif action in ("enable", "disable"):
        conn.execute("UPDATE users SET enabled=? WHERE id=?", (int(action == "enable"), row["id"]))
    if action in ("reset-password", "disable", "revoke-sessions"):
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    conn.commit()


def backup_database(source: Path, destination: Path):
    destination = destination.expanduser().resolve()
    if destination == source.resolve():
        raise ValueError("备份文件不能是原数据库")
    if not source.is_file():
        raise ValueError("数据库不存在，请先初始化")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份完整性检查失败")
    except Exception:
        destination.unlink(missing_ok=True)  # Only the new file created by this function.
        raise
    return destination


def restore_database(source: Path, destination: Path):
    source = source.expanduser().resolve()
    if source == destination.resolve() or not source.is_file():
        raise ValueError("恢复来源必须是一个存在的独立备份文件")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份完整性检查失败")
        if src.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("备份数据库版本与当前程序不匹配")
        # Ensure this is our schema before writing to the destination.
        src.execute("SELECT username_key,password_hash,revision FROM users LIMIT 1")
        src.execute("SELECT user_id,kind,key,version FROM records LIMIT 1")
        src.execute("SELECT token_hash,user_id,expires_at FROM sessions LIMIT 1")
        src.execute("SELECT scope,attempts FROM login_attempts LIMIT 1")
        if src.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("备份包含无效的外键引用")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            # Never reactivate login tokens captured in an old backup.
            dst.execute("DELETE FROM sessions")
            dst.execute("DELETE FROM login_attempts")
            dst.commit()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="LiteGameServer 本机管理工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="初始化数据库／执行已有迁移，不删除数据")
    user = commands.add_parser("user", help="仅本机管理用户")
    actions = user.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="列出用户，不显示密码")
    for action in ("add", "reset-password", "disable", "enable", "revoke-sessions"):
        child = actions.add_parser(action)
        child.add_argument("username")
    backup = commands.add_parser("backup", help="SQLite 在线一致性备份，不覆盖已有文件")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="停服后恢复；覆盖现有数据库并使所有会话失效")
    restore.add_argument("source", type=Path)
    restore.add_argument("--confirm-restore", action="store_true", help="确认已停服并备份当前数据库")
    commands.add_parser("login-limit-clear", help="清除登录限流计数（本机管理员救援）")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        if args.command == "backup":
            print(f"备份完成：{backup_database(settings.db_path, args.destination)}")
            return 0
        if args.command == "restore":
            if not args.confirm_restore:
                raise ValueError("请先停服并备份，再使用 --confirm-restore 确认覆盖")
            restore_database(args.source, settings.db_path)
            print("恢复完成，所有登录已失效。客户端必须清除同步基线并全量重新核对，不能直接沿用旧游标。")
            return 0
        initialize(settings.db_path)
        if args.command == "init-db":
            print(f"数据库已就绪：{settings.db_path}")
            return 0
        with connect(settings.db_path) as conn:
            if args.command == "login-limit-clear":
                conn.execute("DELETE FROM login_attempts")
                print("登录限流计数已清除")
            elif args.action == "list":
                print("ID\t状态\t用户名\t创建时间")
                for row in conn.execute("SELECT id,username,enabled,created_at FROM users ORDER BY id"):
                    print(f"{row['id']}\t{'启用' if row['enabled'] else '禁用'}\t{row['username']}\t{row['created_at']}")
            elif args.action == "add":
                normalize_username(args.username)
                create_user(conn, args.username, read_password())
                print("用户已创建，现在可以从游戏页面登录")
            else:
                password_hash = read_password() if args.action == "reset-password" else None
                update_user(conn, args.username, args.action, password_hash)
                print(f"操作完成：{args.action}")
        return 0
    except (ValueError, OSError, sqlite3.Error, EOFError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
