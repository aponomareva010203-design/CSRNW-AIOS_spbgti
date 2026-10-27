#!/bin/bash
# Двойной клик по этому файлу в Finder открывает Терминал и запускает
# платформу (вся логика — в start_mac.sh рядом).
exec bash "$(dirname "$0")/start_mac.sh"
