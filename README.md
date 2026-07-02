**Travel Concierge Agent — Project Writeup**

**What I Built**

This project is an agentic travel-planning assistant built for the Concierge track. Given a destination, trip length, and a budget in rupees, it produces a complete day-by-day itinerary with real attractions, a budget breakdown, a route sequenced by actual geographic distance, and a packing checklist — deployed as an interactive Streamlit web app, not just a notebook.

The core idea I wanted to demonstrate is the difference between "an LLM that writes travel plans" and "an agent that plans, checks its own work, and fixes it." Most trip-planner submissions in this track will prompt an LLM once and print whatever comes back. This one doesn't. It runs a deterministic plan → verify → fix loop, calls real external tools instead of asking the model to guess at facts, and remembers session context so a follow-up like "make day 2 more relaxed" only touches day 2.

**Architecture**

The system has four distinct tools the agent calls, each doing one job:

Attraction lookup. Gemini 2.5 Flash generates a structured list of real, well-known attractions for the destination city, returned as strict JSON (name, cost in INR, category, best time of day). This is the one place the LLM is used for knowledge retrieval rather than reasoning, and it's cached per city for an hour so repeated runs don't burn API quota.

Geocoding and route sequencing. Every attraction name is looked up against OpenStreetMap's Nominatim API to get real latitude/longitude coordinates — not model-hallucinated ones. From there, a nearest-neighbor algorithm sequences each day's stops by actual distance, so the agent isn't sending someone across a city and back for no reason. If a specific landmark name doesn't geocode cleanly (parenthetical qualifiers like "Exterior Viewing" used to trip this up), the app falls back to the city center rather than dropping the stop or showing a broken result.

Budget math. Cost totaling and budget comparisons are plain Python arithmetic, not the LLM doing sums. The total budget is split across stay, food, transport, and activities using a fixed allocation, and each day's activity spend is checked against its share of that allocation.

Packing list. A deterministic function based on trip length and type — no API call needed for something this simple.

**The Self-Correction Loop**

This is the part I think matters most for showing genuine agent design rather than a prompt wrapper. For each day, the agent:


Picks a small set of attractions, biased toward cheaper options first.
Sequences them by real distance.
Calculates the total cost and checks it against that day's budget allocation.
If it's over budget, it removes the single most expensive item and rechecks.
Repeats until the day is within budget or it runs out of items to drop.


Every step of this loop is logged to a visible trace in the UI, so the reasoning isn't hidden — you can watch the agent notice it's over budget, decide what to cut, and confirm the fix worked. This was a deliberate choice: rather than trusting Gemini's own function-calling loop to reliably execute a multi-step budget-check-and-revise sequence during a live demo, the loop itself is written in plain Python control flow, with Gemini used only for the parts that genuinely need language understanding (generating the initial attraction list). This makes the behavior reproducible and easy to explain, rather than something that might behave differently between runs.

**Memory**

The app keeps a per-day itinerary dictionary in session state rather than a single blob of text. When a user asks to revise one day, only that day's entry is touched — the rest of the trip stays exactly as it was. This is a small thing, but it's the difference between an agent with actual state and one that regenerates everything from scratch on every message, which quickly becomes expensive and inconsistent.

**Handling Real-World API Constraints**

Two practical problems came up building this, and both are handled explicitly rather than papered over:

Rate limiting. Free-tier Gemini keys hit quota limits quickly, especially with an agent making several calls per request. Every model call goes through a retry wrapper with exponential backoff and jitter, so a transient 429 doesn't crash the whole itinerary generation — it waits and retries, and only surfaces an error to the user after several genuine failures.

Nominatim's usage policy. OpenStreetMap's free geocoding service caps requests at roughly one per second and requires a real user-agent string. The geocoding tool respects that pacing and has a fallback chain (cleaned name → raw name → city center) so a single awkward attraction name doesn't break the day's route.

**Deployment and Data Safety**

The app is deployed on Streamlit Community Cloud rather than living only in a notebook, so anyone can open a link and actually use it — type in a city, days, and budget, and get a real itinerary back with a PDF download. This matters for the Concierge track specifically: a tool people can only run by executing notebook cells isn't really a concierge.

On data handling, since "keeping user data secure" is explicitly part of the track brief: the app stores nothing outside of Streamlit's in-memory session state. There's no database, no file writes, no user accounts, and no logging of destinations or budgets beyond the active browser session. The only external calls made with user-provided text go to Google's Gemini API for itinerary generation and to OpenStreetMap for public place coordinates — neither is used to build a profile of the user, and everything is discarded when the session ends.

**What This Demonstrates**

Put together, the project shows an agent that reasons over a genuine constraint (budget) using real tools (geocoding, arithmetic) rather than LLM guesswork, verifies its own output against that constraint, revises when it fails, retains state across a conversation, and ships as something a judge can actually click into and try — with the reasoning trace visible the whole way through, rather than a black box that just prints a plausible-looking itinerary.
