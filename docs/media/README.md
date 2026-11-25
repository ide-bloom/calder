# Recording the demo GIF

`demo.gif` is currently a placeholder. To record the real demo (Windows, after
`python scripts/build_native.py`):

1. Start the syringe-pump simulator on TCP port 5025:

   ```
   uv run python -c "from tests.simulators.base import serve_tcp; from tests.simulators.pump import SyringePump; import time; s = serve_tcp(SyringePump(), port=5025); print(s.address); time.sleep(3600)"
   ```

2. Start a screen recorder (for example ScreenToGif) on the terminal and the vendor app window.

3. Record the test vendor app while operating it (or replay the scripted operator):

   ```
   uv run calder record --app out\vendorapp\x64\CalderVendorApp.exe --args "--protocol pump --tcp 127.0.0.1:5025" --filter "tcp:127.0.0.1:5025" --ui --drive tests\capture\pump_script.json --out sessions\pump.libsession
   ```

4. Infer and review:

   ```
   uv run calder infer sessions\pump.libsession --out sessions\pump.json --name pump
   uv run calder review sessions\pump.json
   ```

   Show the Messages view (the UI-correlated `set_volume`/`set_rate` fields and the
   Start/Stop bindings), then confirm the classification of `start`.

5. Generate and run the generated tests:

   ```
   uv run calder generate sessions\pump.json --out sessions\liberated_pump
   cd sessions\liberated_pump && uv run pytest
   ```

6. Drive the simulator from the generated MCP server, for example with the MCP Inspector:

   ```
   npx @modelcontextprotocol/inspector uv run python -m liberated_pump.mcp_server --resource tcp://127.0.0.1:5025
   ```

   Call `get_dispensed`, then `start` without `confirm` (refused), then with `confirm: true`.

7. Export at about 900 px wide, under 5 MB, and save it as `docs/media/demo.gif`.
