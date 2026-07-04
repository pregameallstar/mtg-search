"""ponytail: one generate() function, two backends. Env-var config, no class."""

import json
import os
import re
import sys


def _parse_json(text):
    """Parse JSON from LLM output, robust against common formatting mistakes."""
    # Strip markdown code fences
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("\n", 1)[0] if "\n" in text else text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fix trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Fix unescaped newlines/tabs inside quoted strings (LLMs sometimes do this)
    def _fix_strings(m):
        inner = m.group(1)
        inner = inner.replace("\t", "\\t").replace("\r", "\\r")
        if inner.count("\n") > 0 and '\\n' not in inner:
            inner = inner.replace("\n", "\\n")
        return '"' + inner + '"'

    text = re.sub(r'"([^"\\]*(?:\\.[^"\\]*)*)"', _fix_strings, text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"llm: JSON parse failed after repair: {e}", file=sys.stderr)
        print(f"llm: raw text (first 2000 chars): {text[:2000]}", file=sys.stderr)
        raise


def generate(system_prompt, user_prompt, backend=None, api_key=None,
             base_url=None, model=None):
    """Send prompt to configured LLM, return parsed JSON dict.

    Args override env vars when provided. Falls back to env vars otherwise.
    """
    backend = backend or os.environ.get("LLM_BACKEND", "openai")
    api_key = api_key or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key configured. Set LLM_API_KEY in the config form below "
            "or as an environment variable."
        )

    if backend == "anthropic":
        model = model or os.environ.get("LLM_MODEL", "claude-sonnet-5")
        return _anthropic(api_key, model, system_prompt, user_prompt)
    else:
        base_url = base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        model = model or os.environ.get("LLM_MODEL", "gpt-4o")
        return _openai(api_key, base_url, model, system_prompt, user_prompt)


def _openai(api_key, base_url, model, system_prompt, user_prompt):
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )
    text = resp.choices[0].message.content
    return _parse_json(text)


def _anthropic(api_key, model, system_prompt, user_prompt):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text
    return _parse_json(text)
