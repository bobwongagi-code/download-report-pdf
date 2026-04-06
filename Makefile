PYTHON ?= python3

.PHONY: test check benchmark

test:
	$(PYTHON) -m unittest discover -s tests

check:
	$(PYTHON) -m py_compile scripts/download_and_ocr.py scripts/benchmark.py

benchmark:
	$(PYTHON) scripts/benchmark.py benchmarks/sample_cases.json
