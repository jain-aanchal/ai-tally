# aider-demo target repo

Fixture used by `make aider-demo`. Three small Python functions in
`string_utils.py` with `pytest` coverage in `test_string_utils.py`. The
functions ship under-implemented on purpose — Aider fixes them during the demo.

Run the tests directly:

```
cd examples/aider-demo/target-repo && pytest -q
```

`../reset.sh` restores this directory to a clean state between demo tasks.
