.PHONY: install test init configure login channels run daemon status reset docker-build up down logs logs-raw

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/pytest

init:
	.venv/bin/telegram-downloader init

configure:
	.venv/bin/telegram-downloader configure

login:
	.venv/bin/telegram-downloader login

channels:
	.venv/bin/telegram-downloader channels

run:
	.venv/bin/telegram-downloader run

daemon:
	.venv/bin/telegram-downloader daemon

status:
	.venv/bin/telegram-downloader status

reset:
	.venv/bin/telegram-downloader reset

docker-build:
	docker compose build

up:
	docker compose up

down:
	docker compose down

logs:
	docker compose logs -f telegram-downloader | grep -vE 'CPU: [0-9.]+% Memory: .* Goroutines: [0-9]+'

logs-raw:
	docker compose logs -f
