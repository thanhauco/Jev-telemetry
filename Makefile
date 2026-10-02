PY ?= .venv/bin/python
JT ?= .venv/bin/jev-telemetry

.PHONY: install test lint demo eval estimate up down replay

install:
	uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python $(PY)

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests

samples/logs.jsonl:
	$(JT) generate-sample --out samples

demo: samples/logs.jsonl
	$(JT) run --input samples/logs.jsonl --changes samples/changes.jsonl

eval: samples/logs.jsonl
	$(JT) eval --golden samples/golden.jsonl --json eval-report.json

estimate: samples/logs.jsonl
	$(JT) estimate --input samples/logs.jsonl

up:
	docker compose -f deploy/docker-compose.yml up -d --build

down:
	docker compose -f deploy/docker-compose.yml down

replay: samples/logs.jsonl
	$(JT) replay --input samples/logs.jsonl
