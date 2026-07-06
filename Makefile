.PHONY: install demo serve sample-smoke sample-replay lint clean

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Use uv if it's on PATH (faster). Otherwise fall back to venv + pip.
UV := $(shell command -v uv 2>/dev/null)

install:
ifdef UV
	uv sync
else
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .
endif

demo:
	./demo/run-demo.sh

serve:
	$(PY) -m verifier.cli serve --port 8000

sample-smoke:
	cd sample-app && PYTHONPATH=. ../$(PY) -m app smoke

sample-replay:
	cd sample-app && PYTHONPATH=. ../$(PY) -m app replay

clean:
	rm -rf demo/out $(VENV) src/verifier/__pycache__ src/verifier.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
