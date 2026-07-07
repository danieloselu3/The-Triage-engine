# Sample Run 2 — Routine case, cloud pathway succeeds

Command:

```
python app.py "Hi, I've had a mild runny nose and a slight cough for one day. No fever. Just want to know if I should rest at home."
```

Output:

```
PATIENT MESSAGE: Hi, I've had a mild runny nose and a slight cough for one day. No fever. Just want to know if I should rest at home.

Pathway used: cloud  |  Latency: 1.50s  |  Prompt version: 3

--- Parsed JSON (validated against AfyaPlus schema) ---
{
  "is_critical_emergency": false,
  "detected_symptoms": [
    "mild runny nose",
    "slight cough"
  ],
  "clinical_reasoning_summary": "Reports mild respiratory symptoms without fever, indicating a non-critical situation.",
  "routing_destination": "Self-Care Information"
}

ROUTING DECISION: [ROUTINE] -> Self-Care Information (via cloud pathway, 1.50s, schema_valid=True)
```

**What this demonstrates:** the system correctly avoids over-triaging a clearly minor complaint, and does not manufacture a diagnosis or medication suggestion for the patient's "should I rest at home" question — it stays within the routing schema instead of answering conversationally.
