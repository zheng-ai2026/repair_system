#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'repair_system.settings')
    # 默认开发端口 6321（未显式指定地址/端口时生效；传了端口则以命令行为准）
    if len(sys.argv) >= 2 and sys.argv[1] == "runserver":
        positional_args = [a for a in sys.argv[2:] if not a.startswith("-")]
        if not positional_args:
            sys.argv.append("127.0.0.1:6321")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
