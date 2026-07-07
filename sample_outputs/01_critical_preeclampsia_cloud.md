# Sample Run 1 — Critical emergency, cloud pathway succeeds

Command:

```
python app.py "Hello AfyaPlus, I am Chidinma. I am 7 months pregnant with my third child. For the past two days, I have had a severe headache that will not go away and my feet are suddenly very swollen. I feel safe waiting for my appointment next week."
```

Output:

```
PATIENT MESSAGE: Hello AfyaPlus, I am Chidinma. I am 7 months pregnant with my third child. For the past two days, I have had a severe headache that will not go away and my feet are suddenly very swollen. I feel safe waiting for my appointment next week.

Pathway used: cloud  |  Latency: 2.52s  |  Prompt version: 3

--- Parsed JSON (validated against AfyaPlus schema) ---
{
  "is_critical_emergency": true,
  "detected_symptoms": [
    "severe headache",
    "sudden swelling of feet"
  ],
  "clinical_reasoning_summary": "Reports symptoms consistent with potential preeclampsia, which requires immediate medical attention.",
  "routing_destination": "Emergency Medical Call Team"
}

ROUTING DECISION: [EMERGENCY] -> Emergency Medical Call Team (via cloud pathway, 2.52s, schema_valid=True)
```

**What this demonstrates:** GPT-4o-mini correctly picks up the two named preeclampsia markers (persistent headache, sudden swelling) despite the patient's own reassurance ("I feel safe waiting"), and does not defer to the patient's self-assessment — it routes to the emergency queue anyway. Output parses with `json.loads()` on the first attempt, no markdown fences, no conversational text.
