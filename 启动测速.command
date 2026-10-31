#!/bin/bash
cd "$(dirname "$0")" || exit 1
for candidate in python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
        "$candidate" launcher.py "$@"
        status=$?
        if [ "$status" -ne 0 ]; then
            printf '\n启动失败。请保留上面的提示，按回车关闭。'
            read -r _
        fi
        exit "$status"
    fi
done
printf '未找到 Python 3.11 或更新版本。请先从 https://www.python.org/downloads/ 安装 Python。\n按回车关闭。'
read -r _
exit 1
