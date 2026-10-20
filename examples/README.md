# Examples

`python3 offline_demo.py` exercises the installed runtime with two fake native CLIs in a disposable HOME. No account, real model or retrieval is used. This proves collection mechanics, not independent reasoning.

`python3 stage_demo.py /tmp/agent-offload/example-1` stages the whole `preferences.py`, `PROMPT.md` and a random proof token. It refuses an existing BASE. Inspect the packet before using it with a real native peer; see [Adoption](../docs/adoption.md).

`config.example.json` is a minimal local roster example, with no credentials. Adapt it to your available model selectors and actual native logins. Do not overwrite an existing local configuration blindly.

`CONSENSUS.example.md` shows the adjudication format with hypothetical findings. It is not output from a live review. Run artifacts and actual private consensus documents stay outside version control.
