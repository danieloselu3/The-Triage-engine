# AfyaPlus Triage Engine

A working prototype of the AfyaPlus health assistant's triage pipeline. It takes an unstructured patient message, runs it through a guardrailed, Chain-of-Thought prompt on GPT-4o-mini, enforces a strict JSON output schema, and automatically falls back to a local Ollama model if the cloud API fails or times out — so the pipeline never crashes, even fully offline.

## The business problem

AfyaPlus's backend routing engine needs predictable, machine-readable triage decisions. Patients send messy, conversational, sometimes multilingual messages. Early testing surfaced three failure modes: models produce conversational fluff instead of structured output, they occasionally hallucinate clinical facts (naming diseases, suggesting treatments), and the pipeline has no plan for what happens when the network degrades. This prototype exists to prove those three failure modes can be engineered around with prompting, schema enforcement, and resilience patterns — before any real patient traffic touches it.

## Architecture

```
                    ┌─────────────────────┐
 patient message -> │  V3 defensive CoT    │
                    │  prompt (role +      │
                    │  reasoning steps +   │
                    │  guardrails)         │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  CLOUD: GPT-4o-mini   │   timeout = 4.0s
                    │  response_format=     │   specific exceptions:
                    │  json_object          │   APITimeoutError,
                    └──────────┬───────────┘   APIConnectionError,
                               │ fails/times out RateLimitError,
                    ┌──────────▼───────────┐   APIStatusError, APIError
                    │  LOCAL: Ollama        │
                    │  llama3.2 (same       │   same OpenAI-compatible
                    │  OpenAI-compatible    │   call shape, different
                    │  call shape)          │   base_url
                    └──────────┬───────────┘
                               │ fails too (rare)
                    ┌──────────▼───────────┐
                    │  Safe default JSON    │   never crashes -
                    │  -> Manual Review     │   errs toward caution
                    └──────────┬───────────┘
                               │
                    strip fences -> json.loads -> schema
                    validation -> keyword guardrail -> print
                    parsed dict + one-line routing decision
```

Both the cloud and local pathways are reached through the same `openai` Python SDK call shape (`client.chat.completions.create(...)`) — only the `base_url`/`api_key`/`model` differ, so the rest of the pipeline doesn't need to know which one answered.

## How to run

```bash
python -m venv triage_venv
# Windows: triage_venv\Scripts\activate    macOS/Linux: source triage_venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in OPENAI_API_KEY

ollama pull llama3.2    # one-time, local pathway
ollama serve             # keep running in a separate terminal

python app.py "patient message here"        # single message
python app.py --demo                        # 3 canned scenarios
python app.py --prompt-version 1 "..."      # see the naive baseline
python app.py --prompt-version 2 "..."      # see the role/template version
python app.py --force-cloud-fail "..."      # genuinely force the local fallback
python app.py --stats                       # cloud-vs-local latency summary
```

If no message is given, a hardcoded preeclampsia test case is used by default.

## Prompt engineering iteration log

Three prompt versions were built and tested against the same recurring test case (a 7-months-pregnant patient reporting a persistent headache and sudden swelling — classic preeclampsia markers). Only V3 is wired into the live pipeline; V1 and V2 are kept in `app.py` and are runnable via `--prompt-version` so the difference is directly demonstrable, not just described. Full transcripts are in `sample_outputs/04_prompt_iteration_comparison.md`.

**V1 — naive zero-shot.** `"Look at this patient message and tell me what's wrong: {message}"`. Result: a conversational, multi-paragraph answer that names specific diseases ("Preeclampsia... Gestational Hypertension...") as a differential diagnosis, includes no structure, and cannot be parsed by a backend at all. This is the exact "conversational fluff + hallucinated clinical facts" failure mode from the problem statement.

**V2 — role-based + fixed template.** Assigns the model the identity of "an expert obstetric and general triage nurse" and forces a `RISK LEVEL / ACTION REQUIRED / SUMMARY` text template. Result: consistent, on-topic, no fluff — but still free text. A backend would need a brittle regex parser, and nothing stops the model from drifting off-template on a harder or adversarial input.

**V3 — defensive Chain-of-Thought + native JSON mode (production).** Built by extending V2 with:
- **Role-based assignment** — an explicit operational identity ("AfyaPlus TriageBot, a defensive, automated triage routing system... not a chatbot") and boundaries ("never diagnose... never prescribe...").
- **Chain-of-Thought reasoning** — five numbered internal steps (extract only stated symptoms → match against named high-risk patterns → decide critical/routine → decide routing destination → write an objective one-sentence summary) that the model works through *before* emitting the JSON, which is what keeps the classification consistent instead of a one-shot guess.
- **Defensive guardrails**, each added for a specific reason observed in V1/V2 testing:
  - *"Do NOT include any conversational openings/greetings/closing remarks"* — added because early tests occasionally got a leading "Sure, here's the analysis:" even with JSON mode enabled.
  - *"Do NOT state a diagnosis as confirmed fact... use 'reports symptoms consistent with...'"* — added directly in response to V1's hallucinated differential-diagnosis list; this is the single most important guardrail line for the "hallucinates clinical facts" failure mode.
  - *"Do NOT calculate or state any medication dosage/drug name/numeric measurement you were not given"* — a blanket ban on the kind of unverified medical calculation the brief explicitly warns about.
  - *"If information is ambiguous, prefer 'General Nurse Queue' over 'Self-Care Information'"* — added so an uncertain model errs toward a human reviewing the case rather than reassuring a patient who might actually be at risk.
  - *"Return ONLY a raw JSON object... no markdown fences"* — added because, per OpenAI's own JSON-mode documentation, `response_format={"type":"json_object"}` alone does not guarantee fence-free output; the instruction plus native JSON mode together get 100% valid, parseable output in cloud testing.
- The prompt also tells the model it may receive English, Swahili, or Sheng input but must always populate the JSON string fields in English, so the fixed downstream schema stays consistent regardless of the patient's language (built for free on top of the CoT prompt, no extra engineering).

## JSON schema enforcement

Native JSON mode (`response_format={"type": "json_object"}`) is set on the cloud request whenever the V3 prompt is used (the OpenAI API requires the word "json" to appear in the prompt for this to be accepted — V1/V2 deliberately don't request JSON mode, since they're free-text demo prompts, not the production pathway). The raw string is passed to `json.loads()`. Two defensive layers sit behind that:

1. `strip_json_fences()` — strips ` ``` ` fences before parsing, in case a model (particularly the local one) still wraps its output despite instructions.
2. `validate_triage_schema()` — checks all four required keys are present with the correct types (`bool`, `list[str]`, `str`, `str`). If validation fails for any reason, the pipeline does not crash or pass bad data downstream — it substitutes a safe default response routed to `"Manual Review Queue"`.

Required schema:

```json
{
  "is_critical_emergency": boolean,
  "detected_symptoms": ["string", "string"],
  "clinical_reasoning_summary": "string",
  "routing_destination": "string"
}
```

There's also a cheap output-side keyword guardrail (`BLOCKED_PATTERNS`) checked against `clinical_reasoning_summary` as a second line of defense — if the model still leaks a phrase like "you have" or "prescribe" despite the prompt-level rules, the summary is sanitised rather than the whole response being discarded.

## API resilience & error handling

- **Hard timeout**: the cloud request is capped at `CLOUD_TIMEOUT = 4.0` seconds (per the project brief).
- **Specific exception handling**, not a single broad `except`: `APITimeoutError`, `APIConnectionError`, `RateLimitError`, `APIStatusError`, and `APIError` are each caught and logged with a distinct message (see `call_cloud`/`call_local` in `app.py`), with a narrow catch-all `Exception` last as a safety net that should rarely fire.
- **Automatic fallback**: any cloud failure raises a single `CloudUnavailableError`, which the orchestrator catches and re-routes the *same* prompt to the local Ollama client — no duplicated call logic, no crash.
- **Ultimate safety net**: if the local pathway also fails (`LocalUnavailableError`), the pipeline returns a hardcoded, schema-valid default response routed to `"Manual Review Queue"` rather than raising. Verified by pointing both pathways at unreachable ports simultaneously — the script exits 0 every time.
- Deliberately **not** implemented: multi-attempt exponential-backoff retries on the cloud call before falling back. The brief's 4-second cap is about bounding user-facing latency; stacking retries within that budget would either blow past 4 seconds or leave no time for the local fallback, so a single fast fail-over was chosen over a retry loop. This is a scope decision, not an oversight — noted here for transparency.

## Cloud vs. local: measured performance

Real numbers from `logs/run_log.jsonl` after the runs in `sample_outputs/`, via `python app.py --stats`:

| Pathway | Runs | Avg latency |
|---|---|---|
| Cloud (GPT-4o-mini) | 9 | 2.08s |
| Local (Ollama llama3.2, CPU) | 2 | 25.01s |
| Safe default (both pathways down) | 1 | 17.29s |

Local inference is roughly **10-15x slower** than cloud on this machine (CPU-only Ollama). That gap is the real, practical cost of the offline fallback — acceptable for "the system doesn't freeze," not acceptable as a steady-state replacement for cloud at any meaningful patient volume.

**More important than the latency gap — a clinical quality gap:** on the exact preeclampsia case that GPT-4o-mini correctly flagged `is_critical_emergency: true` → `Emergency Medical Call Team`, the local `llama3.2` model flagged the *same* case `false` → `General Nurse Queue`, despite its own summary correctly naming the preeclampsia markers. This was reproduced across repeated runs (see `sample_outputs/03_cloud_fail_local_fallback.md`). The local fallback is a genuine engineering success for uptime — the app never crashes — but it is not a clinically equivalent substitute, and this prototype should not be read as proof that offline mode is safe for unattended critical-case triage.

![latency logs](sample_outputs\image.png)

## Sample outputs

See `sample_outputs/` for four full transcripts:
1. `01_critical_preeclampsia_cloud.md` — critical case, cloud succeeds.
2. `02_routine_case_cloud.md` — routine case, cloud succeeds.
3. `03_cloud_fail_local_fallback.md` — cloud genuinely fails (`--force-cloud-fail`), falls back to local Ollama; includes the clinical-quality caveat above.
4. `04_prompt_iteration_comparison.md` — the same case run through V1, V2, and V3 side by side.

## Known limitations / risks / future work

- **No second-pass safety judge.** A separate LLM call that re-checks the first response against explicit safety rules (a pattern the course covers) was deliberately left out of this prototype to keep cost/latency down for a demo; it would be the natural next hardening step before any real patient traffic.
- **No streaming.** Streaming and native JSON-mode parsing don't mix cleanly (partial JSON chunks aren't valid JSON), so this prototype trades streaming UX for reliable structured output.
- **Multilingual support is prompt-level only** — the model is told to understand English/Swahili/Sheng and reply in English, but there's no dedicated language-detection or translation-quality testing here.
- **Single in-process script, no queue/concurrency control.** Fine for a CLI demo; a production version would need request queuing and rate-limit-aware concurrency (the codebase is already async-first via `AsyncOpenAI`, which makes that extension straightforward).
- **Local model clinical reliability**, as documented above, is the biggest operational risk surfaced by this prototype and the one most worth discussing in the business presentation.
