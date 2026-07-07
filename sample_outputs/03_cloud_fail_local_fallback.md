# Sample Run 3 — Cloud fails, genuine fallback to local Ollama

This run uses `--force-cloud-fail`, which points the cloud client at an
unreachable local port (`http://localhost:1/v1`) for this one request only.
This produces a **real** `openai.APIConnectionError`, not a simulated one —
the fallback path below is genuinely exercised, not faked.

Command:

```
python app.py --force-cloud-fail "Hello AfyaPlus, I am Chidinma. I am 7 months pregnant with my third child. For the past two days, I have had a severe headache that will not go away and my feet are suddenly very swollen. I feel safe waiting for my appointment next week."
```

Output:

```
PATIENT MESSAGE: Hello AfyaPlus, I am Chidinma. I am 7 months pregnant with my third child. For the past two days, I have had a severe headache that will not go away and my feet are suddenly very swollen. I feel safe waiting for my appointment next week.
[resilience] Cloud pathway failed (cloud connection error: Connection error.); falling back to local Ollama.

Pathway used: local  |  Latency: 15.94s  |  Prompt version: 3

--- Parsed JSON (validated against AfyaPlus schema) ---
{
  "is_critical_emergency": false,
  "detected_symptoms": [
    "severe headache",
    "sudden swelling of the feet"
  ],
  "clinical_reasoning_summary": "Reports symptoms consistent with preeclampsia markers (persistent headache plus sudden swelling) that warrant immediate attention, but overall presentation does not indicate a critical emergency.",
  "routing_destination": "General Nurse Queue"
}

ROUTING DECISION: [ROUTINE] -> General Nurse Queue (via local pathway, 15.94s, schema_valid=True)
```

**What this demonstrates:** the app does not crash or hang when the cloud pathway is unreachable — it catches the specific `APIConnectionError`, logs why, and automatically re-routes the same prompt to the local `llama3.2` model over Ollama's OpenAI-compatible endpoint. The local model still returns valid, schema-conformant JSON (no crash, no markdown fences).

**Important honest caveat, also logged in the README's risk section:** on this same preeclampsia case that GPT-4o-mini correctly flagged as `is_critical_emergency: true` (see Sample Run 1), the local `llama3.2` model flagged it as `false` and routed to the general queue instead of the emergency queue — even though its own reasoning summary names the correct preeclampsia markers. This is a real, reproducible finding (confirmed across repeated runs), not a one-off fluke, and it is the single most important operational risk this prototype surfaces: **the offline fallback is safe from a systems-crash perspective, but is not clinically equivalent to the cloud pathway, and should not be trusted for final triage decisions on critical cases without a human in the loop.**
