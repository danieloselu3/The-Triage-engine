"""
AfyaPlus Triage Engine
======================
Takes a patient message, runs it through a defensive, guardrailed triage
prompt on GPT-4o-mini (cloud), automatically falls back to a local Ollama
model if the cloud pathway fails or times out, enforces a strict JSON output
schema, and prints a routing decision.

Usage:
    python app.py "patient message here"
    python app.py --demo
    python app.py --prompt-version 1 "patient message here"
    python app.py --force-cloud-fail "patient message here"
    python app.py --stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLOUD_MODEL = os.getenv("MODEL_NAME")
CLOUD_BASE_URL = os.getenv("MODEL_BASE_URL")
CLOUD_API_KEY = os.getenv("OPENAI_API_KEY") or "sk-not-set"

LOCAL_MODEL = os.getenv("LOCAL_MODEL_NAME")
LOCAL_BASE_URL = os.getenv("LOCAL_BASE_URL")

CLOUD_TIMEOUT = 4.0   # seconds - hard cap on cloud transit per project brief
LOCAL_TIMEOUT = 60.0  # local CPU inference is slower; give it more room

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "run_log.jsonl")

# Two client instances sharing the same OpenAI-compatible interface: one for
# the cloud API, one pointed at the local Ollama server. This is the same
# call shape (client.chat.completions.create) for both, so the rest of the
# pipeline does not need to know which pathway it is talking to.
cloud_client = AsyncOpenAI(base_url=CLOUD_BASE_URL, api_key=CLOUD_API_KEY, timeout=CLOUD_TIMEOUT)
local_client = AsyncOpenAI(base_url=LOCAL_BASE_URL, api_key="ollama", timeout=LOCAL_TIMEOUT)


def make_cloud_client(force_fail: bool) -> AsyncOpenAI:
    """Returns the real cloud client, or (for --force-cloud-fail demos only)
    a client pointed at an unreachable local port so the failure and
    fallback path is genuinely exercised rather than simulated.
    """
    if force_fail:
        return AsyncOpenAI(base_url="http://localhost:1/v1", api_key=CLOUD_API_KEY, timeout=CLOUD_TIMEOUT)
    return cloud_client


# ---------------------------------------------------------------------------
# Prompt engineering - three iterations (naive -> role/template -> defensive CoT+JSON)
# ---------------------------------------------------------------------------

def build_prompt_v1_naive(patient_message: str) -> list[dict]:
    """V1 - naive zero-shot. No role, no structure, no guardrails. Kept only
    to document the starting point in the README's iteration log; never used
    in the live pipeline (this is the "No Marks" baseline from the rubric).
    """
    return [
        {"role": "user", "content": f"Look at this patient message and tell me what's wrong: {patient_message}"}
    ]


def build_prompt_v2_role_template(patient_message: str) -> list[dict]:
    """V2 - role-based + fixed text template. More consistent than V1, but
    still free-text (not JSON) with no explicit reasoning steps or
    guardrails, so it can still leak conversational fluff on edge cases.
    """
    system = (
        "You are an expert obstetric and general triage nurse at AfyaPlus Health. "
        "Analyse the patient message for urgent complications, including but not "
        "limited to preeclampsia markers (severe headache, vision changes, sudden "
        "swelling), bleeding, breathing difficulty, or chest pain.\n\n"
        "Provide your output exactly like this:\n"
        "RISK LEVEL: [CRITICAL / NORMAL]\n"
        "ACTION REQUIRED: [IMMEDIATE OUTREACH / STANDARD FOLLOWUP]\n"
        "SUMMARY: [1-sentence explanation]"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": patient_message},
    ]


TRIAGE_JSON_SCHEMA_DESCRIPTION = """{
  "is_critical_emergency": boolean,
  "detected_symptoms": ["string", "string"],
  "clinical_reasoning_summary": "string",
  "routing_destination": "string"
}"""


def build_prompt_v3_defensive_cot_json(patient_message: str) -> list[dict]:
    """V3 - the production prompt: role assignment, explicit Chain-of-Thought
    reasoning steps, defensive guardrails, and native JSON schema
    enforcement. This is the only prompt wired into the live pipeline.
    """
    system = f"""You are AfyaPlus TriageBot, a defensive, automated triage routing system for AfyaPlus Health. You are not a chatbot and you are not having a conversation with the patient.

OPERATIONAL IDENTITY AND BOUNDARIES:
- Your only job is to analyse one incoming patient message and output a single JSON object.
- You are not a doctor. You never diagnose a condition, never state a disease as confirmed fact, and never prescribe or suggest medications, dosages, or specific treatments.
- The patient may write in English, Swahili, or Sheng. Understand any of these, but always write the JSON string field values in clear English so the downstream routing system can process them consistently.

CHAIN-OF-THOUGHT INSTRUCTIONS (internal reasoning - do not output these steps, only the final JSON):
1. Read the message and list only the symptoms actually stated or clearly implied. Do not invent symptoms that were not mentioned.
2. Check those symptoms against known high-risk patterns: preeclampsia markers (persistent headache plus vision changes plus sudden swelling in pregnancy), breathing difficulty, chest pain, heavy bleeding, premature labour, loss of consciousness, or severe trauma.
3. Decide if this is a critical emergency (true) or routine/non-emergency (false) based only on step 2.
4. Decide the routing destination: "Emergency Medical Call Team" for critical emergencies, "General Nurse Queue" for routine or ambiguous cases needing human review, or "Self-Care Information" only for clearly minor, non-urgent complaints.
5. Write a one-sentence, objective clinical_reasoning_summary describing what was observed and why that routing was chosen, without stating a diagnosis as fact.

STRICT OUTPUT RULES (guardrails):
- Do NOT include any conversational openings, greetings, apologies, disclaimers, or closing remarks.
- Do NOT include markdown formatting, code fences, or any text outside the JSON object.
- Do NOT state a diagnosis as confirmed fact. Use language like "reports symptoms consistent with..." rather than "patient has...".
- Do NOT calculate or state any medication dosage, drug name, or numeric clinical measurement you were not explicitly given.
- If information is ambiguous, prefer routing to "General Nurse Queue" over "Self-Care Information" - never downplay an ambiguous case just to appear reassuring.
- Return ONLY a raw JSON object. No markdown fences, no leading or trailing text.

REQUIRED JSON OUTPUT SCHEMA (return exactly these four keys, nothing else):
{TRIAGE_JSON_SCHEMA_DESCRIPTION}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": patient_message},
    ]


PROMPT_BUILDERS = {
    1: build_prompt_v1_naive,
    2: build_prompt_v2_role_template,
    3: build_prompt_v3_defensive_cot_json,
}

# ---------------------------------------------------------------------------
# JSON schema validation & output-side guardrail (Phase 4)
# ---------------------------------------------------------------------------

REQUIRED_SCHEMA = {
    "is_critical_emergency": bool,
    "detected_symptoms": list,
    "clinical_reasoning_summary": str,
    "routing_destination": str,
}

# Cheap keyword safety net behind the prompt-level guardrails - catches the
# rare case where the model leaks a diagnosis/prescription-style phrase
# despite being instructed not to.
BLOCKED_PATTERNS = ["diagnosis is", "you have", "prescribe", "take ", "mg ", "dosage", "dose of"]

SAFE_DEFAULT_RESPONSE = {
    "is_critical_emergency": True,
    "detected_symptoms": ["unable to parse system output"],
    "clinical_reasoning_summary": (
        "Automated triage could not produce a valid structured response for this "
        "message. Routed to manual review as a precaution."
    ),
    "routing_destination": "Manual Review Queue",
}


def strip_json_fences(text: str) -> str:
    """Defensive cleanup: some local models wrap JSON in markdown fences even
    when explicitly told not to. Applied to both pathways for safety.
    """
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) >= 2 else text.lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


def validate_triage_schema(parsed) -> bool:
    if not isinstance(parsed, dict):
        return False
    for key, expected_type in REQUIRED_SCHEMA.items():
        if key not in parsed or not isinstance(parsed[key], expected_type):
            return False
    return all(isinstance(s, str) for s in parsed["detected_symptoms"])


def apply_output_guardrail(parsed: dict) -> dict:
    summary_lower = parsed.get("clinical_reasoning_summary", "").lower()
    if any(pattern in summary_lower for pattern in BLOCKED_PATTERNS):
        print("[guardrail] Blocked pattern detected in model output; sanitising summary.")
        parsed["clinical_reasoning_summary"] = (
            "Summary withheld: contained language resembling a diagnosis or "
            "medication instruction. Flagged for manual review."
        )
    return parsed


# ---------------------------------------------------------------------------
# Resilience: cloud pathway with strict timeout, local Ollama fallback (Phase 2)
# ---------------------------------------------------------------------------

class CloudUnavailableError(Exception):
    """Raised when the cloud pathway cannot complete a request, for any reason."""


class LocalUnavailableError(Exception):
    """Raised when the local Ollama pathway cannot complete a request, for any reason."""


async def call_cloud(messages: list[dict], client: AsyncOpenAI | None = None, json_mode: bool = True) -> str:
    client = client or cloud_client
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        response = await client.chat.completions.create(
            model=CLOUD_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=400,
            timeout=CLOUD_TIMEOUT,
            **kwargs,
        )
        return response.choices[0].message.content
    except APITimeoutError as e:
        raise CloudUnavailableError(f"cloud request timed out after {CLOUD_TIMEOUT}s") from e
    except APIConnectionError as e:
        raise CloudUnavailableError(f"cloud connection error: {e}") from e
    except RateLimitError as e:
        raise CloudUnavailableError(f"cloud rate limited: {e}") from e
    except APIStatusError as e:
        raise CloudUnavailableError(f"cloud returned HTTP {e.status_code}") from e
    except APIError as e:
        raise CloudUnavailableError(f"cloud API error: {e}") from e
    except Exception as e:  # narrow last-resort net; should be rare given the above
        raise CloudUnavailableError(f"unexpected cloud failure: {e}") from e


async def call_local(messages: list[dict], json_mode: bool = True) -> str:
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        response = await local_client.chat.completions.create(
            model=LOCAL_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=400,
            **kwargs,
        )
        return response.choices[0].message.content
    except APITimeoutError as e:
        raise LocalUnavailableError(f"local inference timed out after {LOCAL_TIMEOUT}s") from e
    except APIConnectionError as e:
        raise LocalUnavailableError(f"local Ollama server unreachable at {LOCAL_BASE_URL}: {e}") from e
    except (RateLimitError, APIStatusError, APIError, TypeError):
        # Some Ollama versions/models reject the response_format param outright -
        # retry once in plain-text mode, relying on the strict prompt wording.
        try:
            response = await local_client.chat.completions.create(
                model=LOCAL_MODEL, messages=messages, temperature=0.0, max_tokens=400,
            )
            return response.choices[0].message.content
        except Exception as retry_err:
            raise LocalUnavailableError(f"local retry without JSON mode failed: {retry_err}") from retry_err
    except Exception as e:  # narrow last-resort net; should be rare given the above
        raise LocalUnavailableError(f"unexpected local failure: {e}") from e


async def run_triage_pipeline(
    patient_message: str, prompt_version: int = 3, force_cloud_fail: bool = False
) -> tuple[dict, str, float, bool]:
    """Tries the cloud pathway first (hard 4s timeout). On any cloud failure,
    logs why and falls back to the local Ollama pathway. If both fail, returns
    a safe default response rather than crashing. Returns
    (parsed_response, pathway_used, latency_seconds, schema_valid).
    """
    messages = PROMPT_BUILDERS[prompt_version](patient_message)
    json_mode = prompt_version == 3  # only V3 instructs the model to emit JSON; JSON mode requires that
    start = time.perf_counter()
    pathway = "cloud"
    raw_text = None

    try:
        raw_text = await call_cloud(messages, client=make_cloud_client(force_cloud_fail), json_mode=json_mode)
    except CloudUnavailableError as cloud_err:
        print(f"[resilience] Cloud pathway failed ({cloud_err}); falling back to local Ollama.")
        pathway = "local"
        try:
            raw_text = await call_local(messages, json_mode=json_mode)
        except LocalUnavailableError as local_err:
            print(f"[resilience] Local pathway also failed ({local_err}); using safe default response.")
            pathway = "fallback-default"

    latency = time.perf_counter() - start

    if prompt_version != 3:
        # V1/V2 are free-text demo prompts, not JSON - nothing to parse/validate.
        return {"raw_text": raw_text}, pathway, latency, False

    parsed = None
    if raw_text is not None:
        try:
            parsed = json.loads(strip_json_fences(raw_text))
        except json.JSONDecodeError:
            parsed = None

    valid = parsed is not None and validate_triage_schema(parsed)
    if valid:
        parsed = apply_output_guardrail(parsed)
    else:
        if raw_text is not None:
            print("[schema] Model output failed JSON schema validation; using safe default response.")
        parsed = dict(SAFE_DEFAULT_RESPONSE)

    return parsed, pathway, latency, valid


# ---------------------------------------------------------------------------
# Run logging (feeds the README's cloud-vs-local latency comparison)
# ---------------------------------------------------------------------------

def log_run(patient_message: str, pathway: str, latency: float, is_critical: bool, prompt_version: int) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message_preview": patient_message[:80],
        "pathway": pathway,
        "latency_seconds": round(latency, 3),
        "is_critical_emergency": is_critical,
        "prompt_version": prompt_version,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def print_stats() -> None:
    if not os.path.exists(LOG_PATH):
        print("No runs logged yet. Run the engine a few times first (e.g. python app.py --demo).")
        return

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            pathway = entry["pathway"]
            totals[pathway] = totals.get(pathway, 0.0) + entry["latency_seconds"]
            counts[pathway] = counts.get(pathway, 0) + 1

    print("\n=== Latency Summary (from logs/run_log.jsonl) ===")
    print(f"{'Pathway':<18}{'Runs':<8}{'Avg Latency (s)':<18}")
    for pathway in sorted(totals):
        avg = totals[pathway] / counts[pathway]
        print(f"{pathway:<18}{counts[pathway]:<8}{avg:<18.3f}")


# ---------------------------------------------------------------------------
# CLI / end-to-end demonstration (Phase 5)
# ---------------------------------------------------------------------------

DEMO_SCENARIOS = [
    (
        "Critical - preeclampsia pattern",
        "Hello AfyaPlus, I am Chidinma. I am 7 months pregnant with my third child. "
        "For the past two days, I have had a severe headache that will not go away "
        "and my feet are suddenly very swollen. I feel safe waiting for my "
        "appointment next week.",
    ),
    (
        "Routine - mild cold",
        "Hi, I've had a mild runny nose and a slight cough for one day. No fever. "
        "Just want to know if I should rest at home.",
    ),
    (
        "Ambiguous - intermittent chest discomfort",
        "I'm 45 and I've had a dull ache in my chest on and off since this morning. "
        "It's not too bad and goes away when I sit down. Should I be worried?",
    ),
]

DEFAULT_MESSAGE = DEMO_SCENARIOS[0][1]


async def run_and_report(patient_message: str, prompt_version: int, force_cloud_fail: bool, label: str | None = None) -> None:
    if label:
        print(f"\n{'=' * 70}\nSCENARIO: {label}\n{'=' * 70}")
    print(f"PATIENT MESSAGE: {patient_message}")

    parsed, pathway, latency, valid = await run_triage_pipeline(
        patient_message, prompt_version=prompt_version, force_cloud_fail=force_cloud_fail
    )

    print(f"\nPathway used: {pathway}  |  Latency: {latency:.2f}s  |  Prompt version: {prompt_version}")

    if prompt_version != 3:
        print("\n--- Raw model output (free-text prompt, not JSON-enforced) ---")
        print(parsed.get("raw_text"))
        return

    print("\n--- Parsed JSON (validated against AfyaPlus schema) ---")
    print(json.dumps(parsed, indent=2))

    routing = "EMERGENCY" if parsed["is_critical_emergency"] else "ROUTINE"
    print(
        f"\nROUTING DECISION: [{routing}] -> {parsed['routing_destination']} "
        f"(via {pathway} pathway, {latency:.2f}s, schema_valid={valid})"
    )

    log_run(patient_message, pathway, latency, parsed["is_critical_emergency"], prompt_version)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AfyaPlus Triage Engine")
    parser.add_argument("message", nargs="?", default=None, help="Patient message to triage")
    parser.add_argument("--demo", action="store_true", help="Run three canned demo scenarios")
    parser.add_argument("--prompt-version", type=int, choices=[1, 2, 3], default=3, help="Which prompt iteration to use")
    parser.add_argument(
        "--force-cloud-fail",
        action="store_true",
        help="Deliberately break the cloud pathway to genuinely demonstrate the local fallback",
    )
    parser.add_argument("--stats", action="store_true", help="Print cloud-vs-local latency comparison from past runs and exit")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if args.stats:
        print_stats()
        return

    if args.demo:
        for label, message in DEMO_SCENARIOS:
            await run_and_report(message, args.prompt_version, args.force_cloud_fail, label=label)
        return

    message = args.message or DEFAULT_MESSAGE
    await run_and_report(message, args.prompt_version, args.force_cloud_fail)


if __name__ == "__main__":
    asyncio.run(main())
