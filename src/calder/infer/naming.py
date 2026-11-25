"""Optional, opt-in naming assist through an OpenAI-compatible chat endpoint.

Suggestions land in ``suggested_name`` / ``suggested_doc`` and never replace a
name; the user adopts them in the review UI. Calder works fully offline when
this is not used. The API key is read from an environment variable and never
written to the model.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from calder.model.schema import ProtocolModel

SYSTEM_PROMPT = (
    "You name commands of a laboratory instrument protocol. For each item you get the wire "
    "key, examples, UI labels and manual text. Reply with JSON only: "
    '{"messages": {"<id>": {"name": "snake_case_name", "doc": "one sentence"}}, '
    '"fields": {"<message id>.<field id>": {"name": "snake_case_name", "doc": "one sentence"}}}'
)

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _payload(model: ProtocolModel) -> dict[str, Any]:
    items = []
    for m in model.active_messages():
        items.append(
            {
                "id": m.id,
                "key": m.key,
                "direction": m.direction.value,
                "current_name": m.name,
                "examples": [e.text or e.raw for e in m.examples[:3]],
                "ui": [b.element_name for b in m.ui_bindings],
                "manual": m.manual.description if m.manual else None,
                "fields": [
                    {"id": f.id, "name": f.name, "label": f.label, "units": f.units, "role": f.role.value}
                    for f in m.fields
                    if f.role.value in ("value", "enum", "status")
                ],
            }
        )
    return {"protocol": model.name, "items": items}


def suggest(
    model: ProtocolModel,
    *,
    endpoint: str,
    model_name: str,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> int:
    """Fill suggestions in place; returns how many suggestions were applied."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_payload(model))},
        ],
    }
    url = endpoint.rstrip("/") + "/chat/completions"
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    finally:
        if own:
            http.close()
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    applied = 0
    for mid, sug in (data.get("messages") or {}).items():
        try:
            msg = model.message(mid)
        except KeyError:
            continue
        name = str(sug.get("name", ""))
        if _IDENT.match(name):
            msg.suggested_name = name
            applied += 1
        if sug.get("doc"):
            msg.suggested_doc = str(sug["doc"])[:500]
    for key, sug in (data.get("fields") or {}).items():
        mid, _, fid = str(key).partition(".")
        try:
            fld = model.message(mid).field(fid)
        except KeyError:
            continue
        name = str(sug.get("name", ""))
        if _IDENT.match(name):
            fld.suggested_name = name
            applied += 1
        if sug.get("doc"):
            fld.suggested_doc = str(sug["doc"])[:500]
    return applied
