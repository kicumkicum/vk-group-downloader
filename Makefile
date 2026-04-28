.PHONY: venv install run clean help

# Пути
VENV_DIR := venv
PYTHON := $(VENV_DIR)/bin/python
PIP := $(VENV_DIR)/bin/pip

# Цель по умолчанию
help:
	@echo "VK Audio Downloader - Доступные команды:"
	@echo ""
	@echo "  make venv       - Создать виртуальное окружение"
	@echo "  make install    - Установить зависимости"
	@echo "  make run        - Запустить скрипт"
	@echo "  make get-token  - Получить токен через OAuth"
	@echo "  make get-kate   - Получить аудио-токен Kate Mobile"
	@echo "  make clean      - Удалить виртуальное окружение"
	@echo ""
	@echo "Примеры:"
	@echo "  make venv && make install && make run"
	@echo ""

# Создание виртуального окружения
venv:
	@echo "Создание виртуального окружения..."
	python3 -m venv $(VENV_DIR)
	@echo "Виртуальное окружение создано в $(VENV_DIR)/"
	@echo "Активируйте его: source $(VENV_DIR)/bin/activate"

# Установка зависимостей
install: venv
	@echo "Установка зависимостей..."
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "Зависимости установлены."

# Запуск скрипта
run: install
	@echo "Запуск скрипта..."
	$(PYTHON) vk_audio_downloader.py

# Установка и запуск (одной командой)
all: install run

# Получение токена
get-token:
	$(PYTHON) get_token.py

get-kate:
	$(PYTHON) get_kate_token.py

# Очистка
clean:
	@echo "Удаление виртуального окружения..."
	rm -rf $(VENV_DIR)
	@echo "Готово."
