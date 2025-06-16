#!/bin/bash

# Путь к вашему виртуальному окружению
VENV_PATH="/root/venv"

# Проверяем, существует ли виртуальное окружение
if [ ! -d "$VENV_PATH" ]; then
    echo "Ошибка: Виртуальное окружение не найдено по пути $VENV_PATH"
    echo "Пожалуйста, проверьте путь или создайте виртуальное окружение."
    exit 1
fi

echo "Активирую виртуальное окружение: $VENV_PATH"
source "$VENV_PATH/bin/activate"

echo "Обновляю yt-dlp..."
python -m pip install --upgrade yt-dlp

# Деактивируем виртуальное окружение
deactivate

echo "Обновление yt-dlp завершено."