PYTHON ?= python3

.PHONY: test check benchmark secrets

test:
	$(PYTHON) -m unittest discover -s tests

check:
	$(PYTHON) -m py_compile scripts/*.py

benchmark:
	$(PYTHON) scripts/benchmark.py benchmarks/sample_cases.json

secrets:
	@echo "Secret scanning runs in GitHub Actions via gitleaks."
