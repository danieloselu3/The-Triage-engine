# Sample Run 4 (bonus) — Same case, three prompt iterations

Same patient message run through all three prompt versions via `--prompt-version {1,2,3}`, showing the actual before/after effect of the guardrail work described in the README's prompt-iteration log.

Patient message used for all three:
> "I've had a severe headache for two days and my feet are suddenly swollen, I'm 7 months pregnant"

## V1 — naive zero-shot (`--prompt-version 1`)

```
Pathway used: cloud  |  Latency: 4.11s  |  Prompt version: 1

--- Raw model output (free-text prompt, not JSON-enforced) ---
The patient is experiencing a severe headache and sudden swelling of the feet at 7 months pregnant, which could be concerning for several reasons. In pregnancy, severe headaches and sudden swelling can be signs of conditions such as:

1. **Preeclampsia**: This is a pregnancy complication characterized by high blood pressure and signs of damage to other organ systems, often the kidneys. Symptoms can include severe headaches, swelling, and changes in vision.

2. **Gestational Hypertension**: Elevated blood pressure during pregnancy can also lead to headaches and swelling.

3. **Other Conditions**: While less common, other conditions could also cause these symptoms, such as kidney issues or other systemic problems.

Given the combination of severe headache and sudden swelling, it is important for the patient to seek medical attention promptly to rule out serious complications and ensure the health of both the mother and the baby.
```

Problems: conversational fluff, a bulleted differential diagnosis (exactly the "unverified medical calculation"/diagnosis behaviour the brief warns against), not machine-parseable, no routing decision at all.

## V2 — role-based + fixed template (`--prompt-version 2`)

```
Pathway used: cloud  |  Latency: 2.12s  |  Prompt version: 2

--- Raw model output (free-text prompt, not JSON-enforced) ---
RISK LEVEL: CRITICAL  
ACTION REQUIRED: IMMEDIATE OUTREACH  
SUMMARY: The patient is experiencing severe headache and sudden swelling of the feet, which are concerning signs of potential preeclampsia and require immediate medical attention.
```

Problems: much more consistent and no fluff, but still free-text — a backend consuming this would need a fragile regex/string parser, and there is no native guarantee the model won't deviate from the template on a harder case.

## V3 — defensive CoT + native JSON mode (`--prompt-version 3`, the production prompt)

```
Pathway used: cloud  |  Latency: ~2s  |  Prompt version: 3

--- Parsed JSON (validated against AfyaPlus schema) ---
{
  "is_critical_emergency": true,
  "detected_symptoms": ["severe headache", "sudden swelling of feet"],
  "clinical_reasoning_summary": "Reports symptoms consistent with potential preeclampsia, which requires immediate medical attention.",
  "routing_destination": "Emergency Medical Call Team"
}
```

This is the only version that: (a) is guaranteed to be valid JSON via native `response_format={"type": "json_object"}` JSON mode, (b) passes `json.loads()` and the app's own `validate_triage_schema()` check every time, and (c) never states a diagnosis as confirmed fact ("consistent with potential preeclampsia" vs V1's flat list of named diseases).
