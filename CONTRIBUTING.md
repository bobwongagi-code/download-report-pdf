# Contributing

Thanks for contributing to `url-pdf-download-ocr`.

## Best Ways To Help

- submit sanitized failing URLs or reproducible provider samples
- improve provider-specific rewrite or extraction rules
- improve benchmark coverage and regression cases
- improve documentation and onboarding

## Development Setup

Requirements:

- Python 3
- `curl`
- access to a compatible PaddleOCR script if you want to test end-to-end OCR

Useful commands:

```bash
make test
make check
make benchmark
```

Equivalent direct commands:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/*.py
python3 scripts/benchmark.py benchmarks/sample_cases.json
```

## Provider Rule Changes

When you change provider handling:

1. Add or update a benchmark case when possible.
2. Include the provider name and sample characteristics in your PR description.
3. Prefer simple rewrites and explicit extraction logic over broad heuristics.
4. Avoid claiming support for flows that still require login, captcha, extraction codes, or interactive browser state.

## Benchmark Cases

Each case in `benchmarks/sample_cases.json` can include:

- `name`
- `url`
- `input_type`
- `expected_provider`
- `expected_outcome`
- `notes`

Use `notes` to record why the sample matters, whether it is stable, and whether the URL is sanitized.

## Bug Reports

The highest-signal bug reports include:

- the provider or host involved
- whether the URL is direct, redirected, or a share page
- expected outcome
- actual output JSON
- benchmark summary or regression details if available

If you cannot share the original URL, provide a sanitized sample and the response characteristics instead.

## Pull Requests

- Keep changes focused.
- Add or update tests when behavior changes.
- Update docs when usage, configuration, or benchmark semantics change.
- Mention any new environment variables or assumptions explicitly.

## Code Style

- Keep solutions simple and explicit.
- Prefer standard library code over new dependencies.
- Avoid large abstractions for narrow provider rules.
