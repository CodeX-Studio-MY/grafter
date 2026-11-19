## Summary

<!-- What changed and why. One change per PR. -->

## TODO items

<!-- TODO.md ids covered by this PR, e.g. 0A3, 0A4. -->

-

## Verification

<!-- Paste the output of the checks below. -->

```text
ruff check . && ruff format --check . && mypy && pytest -q
```

## Checklist

- [ ] Tests written first (or updated) for the change
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] `pytest -q` passes
- [ ] TODO.md boxes ticked for the items above
- [ ] No pilot-solution code, client names or client data
- [ ] Any copied code is MIT/Apache, keeps its notice, and is listed in `THIRD_PARTY.md`
