"""
Travel Concierge Agent — Kaggle Capstone (Concierge Track)
------------------------------------------------------------
Run locally:   streamlit run app.py
Deploy:        push to GitHub, deploy on Streamlit Community Cloud,
                set GEMINI_API_KEY in the app's Secrets.

DATA SAFETY NOTE (Concierge track requirement):
This app does not persist any user data to disk or a database. Budget,
destination, and preferences live only in Streamlit's in-memory
st.session_state for the duration of the browser session, and are
discarded when the session ends or the tab is closed. No PII is
collected. The only external call made with user-provided text is to
Google's Gemini API (for itinerary generation) and OpenStreetMap's
Nominatim (for public place coordinates) — neither is used to store
or profile the user.
"""

import os
import json
import time
import requests
import streamlit as st
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, InternalServerError

from gemini_utils import with_retry, GeminiRateLimitError
from pdf_utils import build_itinerary_pdf

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Travel Concierge Agent", page_icon="🧭", layout="wide")

API_KEY = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", None)
if not API_KEY:
    st.error("No GEMINI_API_KEY found. Add it under Settings → Secrets (Streamlit Cloud) "
              "or as an environment variable.")
    st.stop()

genai.configure(api_key=API_KEY)
MODEL_NAME = "gemini-2.5-flash"

if "itinerary" not in st.session_state:
    st.session_state.itinerary = {}       # {day_number: {...}} -- memory, per-day
if "trip_meta" not in st.session_state:
    st.session_state.trip_meta = {}       # city, days, budget, prefs
if "log" not in st.session_state:
    st.session_state.log = []             # visible "agent reasoning" trace for the demo


def log(msg):
    st.session_state.log.append(msg)


# ---------------------------------------------------------------------------
# TOOL 1 — Real geocoding + distance (OpenStreetMap Nominatim, free, no key)
# ---------------------------------------------------------------------------
def _clean_place_name(place: str) -> str:
    """Strip qualifiers like '(Exterior Viewing)' that aren't real geocodable
    places — Nominatim searches for landmarks, not visit-style descriptions."""
    import re
    cleaned = re.sub(r"\([^)]*\)", "", place)  # drop anything in parentheses
    cleaned = re.split(r"[-–—:]", cleaned)[0]   # drop trailing " - stroll" etc.
    return cleaned.strip()


@with_retry(max_retries=3, base_delay=1.5)
def _nominatim_lookup(query: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "kaggle-capstone-travel-agent (student project, contact: your_email@example.com)"}
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def geocode(place: str, city: str):
    """
    Look up real lat/lon for a place. Returns (lat, lon) or None.
    Tries the cleaned name first (strips '(...)' qualifiers), falls back to
    the raw name, then falls back to the city center so a stop is never
    silently dropped from route-sequencing just because of a fussy name.
    Respects Nominatim's ~1 req/sec usage policy with a small delay.
    """
    time.sleep(1.0)  # Nominatim usage policy: max 1 request/second

    for query in [f"{_clean_place_name(place)}, {city}", f"{place}, {city}"]:
        try:
            data = _nominatim_lookup(query)
        except Exception:
            data = None
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])

    # Last resort: city center, so the stop still gets sequenced sensibly
    # instead of showing as "location not found."
    try:
        time.sleep(1.0)
        data = _nominatim_lookup(city)
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def haversine_km(p1, p2):
    """Real distance in km between two (lat, lon) points."""
    from math import radians, sin, cos, sqrt, atan2
    lat1, lon1 = p1
    lat2, lon2 = p2
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def sequence_by_distance(places_with_coords, start=None):
    """Greedy nearest-neighbor ordering so a day's stops aren't zig-zagged."""
    remaining = places_with_coords[:]
    ordered = []
    current = start or remaining[0]
    if start is None:
        ordered.append(remaining.pop(0))
        current = ordered[0]
    while remaining:
        remaining.sort(key=lambda p: haversine_km(current["coords"], p["coords"]))
        nxt = remaining.pop(0)
        ordered.append(nxt)
        current = nxt
    return ordered


# ---------------------------------------------------------------------------
# TOOL 2 — Attraction lookup (Gemini, cached per city to cut down on repeat calls)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def get_attractions(city: str):
    """Returns list of dicts: name, cost (INR), type, time, and geocoded coords."""
    model = genai.GenerativeModel(MODEL_NAME)
    prompt = f"""List 8 real, well-known attractions/activities in {city}.
For each: name, approximate cost in INR per person (0 if free), type
(sightseeing/activity/food), best time of day (morning/afternoon/evening).
Respond ONLY with valid JSON: a list of objects with keys name, cost, type, time."""

    @with_retry(max_retries=5)
    def _call():
        return model.generate_content(prompt)

    result = _call()
    text = result.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1) if text.startswith("json\n") else text
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]") + 1
        items = json.loads(text[start:end])

    # Real tool call, not LLM guessing: geocode each place for actual distances.
    for item in items:
        coords = geocode(item["name"], city)
        item["coords"] = coords if coords else None
    return items


# ---------------------------------------------------------------------------
# TOOL 3 — Budget math (pure Python, not LLM arithmetic)
# ---------------------------------------------------------------------------
def allocate_budget(total_budget: float, days: int):
    """Split budget across categories. Simple, transparent, adjustable."""
    return {
        "stay": round(total_budget * 0.35),
        "food": round(total_budget * 0.25),
        "transport": round(total_budget * 0.15),
        "activities": round(total_budget * 0.25),
    }


def calculate_total_cost(selected_attractions):
    return sum(a["cost"] for a in selected_attractions)


def check_budget(planned_total, budget):
    if planned_total > budget:
        return {"status": "OVER_BUDGET", "amount_over": planned_total - budget}
    return {"status": "WITHIN_BUDGET", "amount_remaining": budget - planned_total}


# ---------------------------------------------------------------------------
# TOOL 4 — Packing list (deterministic, no API call needed)
# ---------------------------------------------------------------------------
def suggest_packing_list(days: int, trip_type: str = "city"):
    base = ["ID/Aadhaar card", "Phone charger", "Power bank",
            "Comfortable walking shoes", "Reusable water bottle"]
    if days >= 3:
        base += [f"Extra clothes for {days} days", "Small first-aid kit"]
    if trip_type == "city":
        base += ["Light jacket for evenings", "Umbrella (seasonal)"]
    if trip_type == "beach":
        base += ["Sunscreen", "Swimwear", "Sunglasses"]
    return base


# ---------------------------------------------------------------------------
# AGENT CORE — explicit plan → verify → fix loop
# ---------------------------------------------------------------------------
def build_day_plan(city, day_number, attractions_pool, day_budget, trip_type, max_fix_rounds=4):
    """
    Deterministic self-correction loop for a single day:
      1. Pick attractions greedily until roughly filling the day.
      2. Sequence them by real distance.
      3. Check cost against day_budget.
      4. If over budget, drop the most expensive non-essential item and recheck.
    Returns the day plan + a trace of what the agent did (for the demo).
    """
    trace = []
    pool = sorted(attractions_pool, key=lambda a: a["cost"])  # cheap-first bias
    picks = pool[:3] if len(pool) >= 3 else pool[:]
    trace.append(f"Day {day_number}: initial picks = {[p['name'] for p in picks]}")

    for round_i in range(max_fix_rounds):
        total = calculate_total_cost(picks)
        result = check_budget(total, day_budget)
        trace.append(f"Day {day_number}: round {round_i+1} — cost ₹{total} vs "
                      f"budget ₹{day_budget} → {result['status']}")
        if result["status"] == "WITHIN_BUDGET":
            break
        # fix: drop most expensive non-essential pick
        if len(picks) <= 1:
            break
        most_expensive = max(picks, key=lambda a: a["cost"])
        picks.remove(most_expensive)
        trace.append(f"Day {day_number}: over budget — dropped '{most_expensive['name']}' "
                      f"(₹{most_expensive['cost']})")

    coords_picks = [p for p in picks if p.get("coords")]
    no_coords = [p for p in picks if not p.get("coords")]
    sequenced = sequence_by_distance(coords_picks) if len(coords_picks) > 1 else coords_picks
    ordered = sequenced + no_coords

    return {
        "day": day_number,
        "stops": ordered,
        "total_cost": calculate_total_cost(ordered),
        "budget": day_budget,
        "trace": trace,
    }


def plan_trip(city, days, total_budget, trip_type):
    st.session_state.log = []
    log(f"Agent received request: {days} days in {city}, budget ₹{total_budget}.")

    allocation = allocate_budget(total_budget, days)
    log(f"Budget allocated (tool call, pure Python): {allocation}")

    attractions = get_attractions(city)
    log(f"Fetched {len(attractions)} real attractions for {city} (Gemini tool call, cached).")

    day_budget = allocation["activities"] / days
    pool = attractions[:]
    plans = {}
    for d in range(1, days + 1):
        day_plan = build_day_plan(city, d, pool, day_budget, trip_type)
        plans[d] = day_plan
        st.session_state.log.extend(day_plan["trace"])
        used_names = {s["name"] for s in day_plan["stops"]}
        pool = [a for a in pool if a["name"] not in used_names]  # don't repeat across days

    st.session_state.itinerary = plans
    st.session_state.trip_meta = {
        "city": city, "days": days, "budget": total_budget,
        "trip_type": trip_type, "allocation": allocation,
    }
    log("Self-correction loop complete for all days. Itinerary finalized.")


def revise_day(day_number, instruction):
    """
    Memory-aware edit: only regenerate the specified day, keep the rest
    of session_state.itinerary untouched.
    """
    meta = st.session_state.trip_meta
    if not meta:
        return
    log(f"Revising only Day {day_number} per instruction: '{instruction}' "
        f"(other days left untouched — session memory).")

    if "relax" in instruction.lower():
        current = st.session_state.itinerary[day_number]
        if len(current["stops"]) > 1:
            dropped = current["stops"].pop()
            current["total_cost"] = calculate_total_cost(current["stops"])
            log(f"Day {day_number}: made lighter — removed '{dropped['name']}'.")
    st.session_state.itinerary[day_number] = current if "relax" in instruction.lower() else st.session_state.itinerary[day_number]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("🧭 Travel Concierge Agent")
st.caption("Kaggle Capstone — Concierge Track · plan → verify → fix, with real tool calls")

with st.expander("🔒 Data safety (Concierge track requirement)", expanded=False):
    st.write(
        "Nothing you enter here is written to a database or file. Your destination, "
        "budget, and preferences stay in this browser session's memory only, and are "
        "cleared when the session ends. No account, no tracking, no persistent storage."
    )

col1, col2 = st.columns([1, 1])
with col1:
    city = st.text_input("Destination", "Goa")
    days = st.number_input("Number of days", min_value=1, max_value=14, value=2)
with col2:
    budget = st.number_input("Total budget (₹)", min_value=500, value=6000, step=500)
    trip_type = st.selectbox("Trip type", ["city", "beach", "adventure"])

if st.button("Plan my trip", type="primary"):
    with st.spinner("Agent is planning, checking budget, and self-correcting..."):
        try:
            plan_trip(city, int(days), float(budget), trip_type)
        except GeminiRateLimitError as e:
            st.error(f"Gemini API is rate-limited right now: {e}\n\nTry again in a minute — "
                      "this is a free-tier quota limit, not a bug.")
        except Exception as e:
            st.error(f"Something went wrong: {e}")

if st.session_state.itinerary:
    meta = st.session_state.trip_meta
    st.subheader(f"{meta['city']} — {meta['days']} days — ₹{meta['budget']:.0f} budget")

    st.markdown("**Budget breakdown**")
    alloc = meta["allocation"]
    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("Stay", f"₹{alloc['stay']}")
    bc2.metric("Food", f"₹{alloc['food']}")
    bc3.metric("Transport", f"₹{alloc['transport']}")
    bc4.metric("Activities", f"₹{alloc['activities']}")

    for d, plan in st.session_state.itinerary.items():
        with st.container(border=True):
            st.markdown(f"### Day {d}")
            st.caption(f"Cost: ₹{plan['total_cost']:.0f} / ₹{plan['budget']:.0f} budget "
                       f"— {'✅ within budget' if plan['total_cost'] <= plan['budget'] else '⚠️ tight'}")
            for i, stop in enumerate(plan["stops"], 1):
                st.write(f"{i}. **{stop['name']}** — {stop['type']}, {stop['time']}, "
                         f"₹{stop['cost']}")

    st.markdown("**Packing checklist**")
    for item in suggest_packing_list(meta["days"], meta["trip_type"]):
        st.write(f"- {item}")

    st.divider()
    st.markdown("#### Ask the agent to revise a day (memory-aware)")
    rc1, rc2 = st.columns([1, 3])
    with rc1:
        revise_day_num = st.selectbox("Day", list(st.session_state.itinerary.keys()), key="revise_day")
    with rc2:
        instruction = st.text_input("Instruction", "make this day more relaxed")
    if st.button("Apply revision"):
        revise_day(revise_day_num, instruction)
        st.rerun()

    with st.expander("🔍 Agent reasoning trace (plan → verify → fix)"):
        for line in st.session_state.log:
            st.text(line)

    pdf_buffer = build_itinerary_pdf(
        meta, st.session_state.itinerary, suggest_packing_list(meta["days"], meta["trip_type"])
    )
    st.download_button(
        "Download itinerary (PDF)", pdf_buffer,
        file_name=f"{meta['city']}_itinerary.pdf", mime="application/pdf"
    )
