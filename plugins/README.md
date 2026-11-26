# Plugin package fixtures

This directory contains the TASK-023 **format and offline verification** fixtures,
not a production plugin registry.

- `samples/echo/`: source directory for the sample plugin.
- `samples/echo.mko`: prebuilt signed sample package.
- `trust/test-root.json`: public test trust root used by the sample.
- `trust/revocations.json`: local offline revocation list (currently empty).

The sample was signed by a throwaway test key; the private key is intentionally
not committed. Treat the test root as a fixture only. See
[`docs/PLUGIN_PACKAGE.md`](../docs/PLUGIN_PACKAGE.md) for the format, commands and
error contract.

Verify the sample:

```powershell
./.venv/Scripts/python.exe scripts/plugin_package.py verify `
  plugins/samples/echo.mko `
  --trust plugins/trust/test-root.json `
  --revocations plugins/trust/revocations.json `
  --host-version 1.4.0
```

The verifier only reads and hashes bytes; it never imports or executes payload
files.