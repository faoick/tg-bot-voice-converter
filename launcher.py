"""First-run setup for a local bot; uses only the Python standard library."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def main() -> int:
    environment = PROJECT_DIR / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if not python.is_file():
            print("Создаю окружение Python…", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        installed = subprocess.run(
            [str(python), "-c", "import telegram, dotenv; assert telegram.__version__ == '22.8'"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if installed.returncode:
            print("Устанавливаю зависимости…", flush=True)
            subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(PROJECT_DIR / "requirements.txt")],
                check=True,
            )
        config = PROJECT_DIR / ".env"
        if not config.exists():
            shutil.copyfile(PROJECT_DIR / ".env.example", config)
        config_text = config.read_text(encoding="utf-8-sig")
        match = re.search(r"^BOT_TOKEN[ \t]*=[ \t]*([^\r\n]*)$", config_text, re.MULTILINE)
        file_token = match.group(1).strip().strip("\"'") if match else ""
        if not os.getenv("BOT_TOKEN", "").strip() and not file_token:
            print("Создайте бота: https://t.me/BotFather → /newbot.")
            print("Токен сохранится только в локальном файле .env.")
            token = getpass.getpass("Вставьте токен (ввод скрыт) и нажмите Enter: ").strip()
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                print("Токен не введён или имеет неверный формат. Запустите ещё раз.")
                return 1
            token_line = f"BOT_TOKEN={token}"
            if match:
                config_text = config_text[:match.start()] + token_line + config_text[match.end():]
            else:
                config_text += "\n" + token_line + "\n"
            config.write_text(config_text, encoding="utf-8")
        return subprocess.run([str(python), str(PROJECT_DIR / "bot.py")], cwd=PROJECT_DIR).returncode
    except (KeyboardInterrupt, EOFError):
        print("Запуск остановлен.")
        return 0
    except (OSError, subprocess.CalledProcessError):
        print("Не удалось подготовить запуск. Проверьте Python, интернет и сообщения выше.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
