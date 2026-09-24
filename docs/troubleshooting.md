# Troubleshooting the local quickstart

| Symptom | Check |
| --- | --- |
| `python3.12` is missing | Install Python 3.12 or newer with `venv` support, then create a fresh virtual environment. `python3 --version` must report at least 3.12. |
| Installation fails | Confirm network access to the Python package index and use the wheel built from this repository. The default install needs no model download. |
| `--init-token` says the file exists | Reuse the existing owner-only key for the same private instance. Do not overwrite a key for an active console. |
| Login fails | Read the local key file directly, without a trailing newline or spaces. Use `http://127.0.0.1:8765/` on the same machine. |
| Browser cannot connect | Check that the console process remains running, port 8765 is free, and the URL uses `127.0.0.1`, not another hostname. |
| Empty workbench | Run the synthetic `create-case` step and refresh. An empty `Bug 研发` page is expected without an imported Project Bug. |
| Project actions unavailable | Verify `project_integration` reader settings, pinned executable digest, dedicated profile, actual space and type keys, item permissions, and worker status. The demo does not start a Project worker. |

Run `k3-supportctl --config "$HOME/.config/k3-support/config.yaml" health`
with the same virtual environment for local database/configuration diagnostics.
Do not post its raw output if it contains instance-specific information.
