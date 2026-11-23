"""Делает хэш пароля для входа в BiletY (переменная BILETY_PASSWORD_HASH).

Запуск:  python scripts/hash_password.py
Пароль вводится скрыто; на экран выводится только хэш — его и вставляй в секрет GitHub BILETY_PASSWORD_HASH.
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import hash_password  # noqa: E402

if __name__ == "__main__":
    pw = getpass.getpass("Пароль для BiletY: ")
    if len(pw) < 6:
        sys.exit("Пароль слишком короткий (нужно хотя бы 6 символов)")
    if getpass.getpass("Ещё раз: ") != pw:
        sys.exit("Пароли не совпали")
    print(hash_password(pw))
