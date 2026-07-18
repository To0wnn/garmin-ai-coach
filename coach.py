#!/usr/bin/env python3
"""Shared logic for garmin-ai-coach's advice pipeline: prompt text, Discord
embed/posting, coach_log persistence, and adherence scoring — all pure
functions of their arguments, with no data-source dependency. The actual
metrics query layer lives in build_metrics_sqlite.py (SQLite-backed);
coach_sqlite.py is the real entry point that ties this module's prompt/
Discord/logging logic together with that query layer.

InfluxDB is gone from this file (finished as part of the multi-user work —
see the migration plan's Stage 5): every function here now runs regardless
of which user it's called for, with no InfluxDB env vars, connections, or
per-device filtering left. That query logic lived here only while coach.py
itself was still the production entry point during the InfluxDB->SQLite
migration's Stage 6 soak period; coach_sqlite.py has been the sole live path
since the cutover, so the InfluxDB-only functions (build_metrics,
_activities_since, daily_stats_window, etc.) were dead code kept around
only for reference — removed outright rather than carried into the
multi-user world, where they'd be meaningless anyway (no InfluxDB data
exists for a newly registered user)."""

import json
import os
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import settings as _settings
from providers import get_provider

# TEMPORARY: user_id hardcoded to 1 (the pre-existing single user) until a
# real CoachContext replaces these process-global constants with per-user
# values read per-invocation — see the multi-user plan. Not done in this pass
# to avoid conflating "remove dead InfluxDB code" with "rewrite the settings
# model" in one diff.
_SETTINGS = _settings.read_settings(1)

# Dashboard-editable (settings.py / coach.db's settings table) — not read
# from .env directly anymore, so a provider/language/watch-device/webhook
# change on the settings page takes effect on the next run without a
# container restart.
# per-user instead of process-global; not done in this pass to avoid
# conflating "remove dead InfluxDB code" with "rewrite the settings model" in
# one diff.
PROVIDER = _SETTINGS["provider"]
LANGUAGE = _SETTINGS["language"]  # e.g. English, Nederlands, Deutsch
DISCORD_WEBHOOK_URL = _SETTINGS["discord_webhook_url"]
WATCH_DEVICE = _SETTINGS["watch_device"]
LOCAL_TZ = ZoneInfo(_SETTINGS["local_tz"])
NOW_LOCAL = datetime.now(LOCAL_TZ)
IS_WEEKLY = NOW_LOCAL.weekday() == 6  # Sunday, in lokale tijd
LOG_HISTORY_DAYS = 14


def owner_log_file(user_id: int) -> str:
    """Per-user coach_log.json path — the coach's own advice-history file
    must not be shared between users (each user gets their own advice, own
    history, own adherence tracking). Persisted under ~/users/<id>/ on the
    coach-home volume so it survives container restarts/rebuilds. Named
    owner_log_file for consistency with owner_output_file/owner_lock_file,
    though it's keyed by user_id specifically (not the effective AI-session
    owner) — advice history belongs to the user who received it, regardless
    of whose AI session generated it."""
    path = os.path.expanduser(f"~/users/{user_id}")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "coach_log.json")

SPORT_TYPES = {
    "running": ["running"],
    "cycling": ["road_biking", "indoor_cycling", "mountain_biking", "gravel_cycling", "cycling"],
    "walking": ["walking", "hiking"],
}

# sport key -> the short field prefix used throughout the AI JSON schema
# (run_tip/bike_tip/walk_tip, run_target/bike_target/walk_target, etc.)
SPORT_FIELD = {"running": "run", "cycling": "bike", "walking": "walk"}


def owner_output_file(owner_id: int) -> str:
    """Per-AI-session-owner output file (see the multi-user plan's sharing
    model) — two owners' independent sessions writing/reading concurrently
    must not collide."""
    return f"/app/output/{owner_id}/advies.json"


def owner_lock_file(owner_id: int) -> str:
    """Per-AI-session-owner cross-process lock — two INDEPENDENT owners' runs
    must be able to proceed concurrently (each has their own tmux pane), while
    two users sharing the SAME owner's session still correctly serialize
    through this one file."""
    return f"/tmp/coach-{owner_id}.lock"


def _stddev(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    mean_val = sum(vals) / len(vals)
    variance = sum((v - mean_val) ** 2 for v in vals) / (len(vals) - 1)
    return variance**0.5


def _format_pace(decimal_min_per_km: float) -> str:
    total_seconds = round(decimal_min_per_km * 60)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


FTP_MIN_LAP_SECONDS = 18 * 60  # long enough to be a meaningful sustained-effort proxy for a 20-min FTP test
FTP_FROM_20MIN_FACTOR = 0.95  # standard estimate: FTP ≈ 95% of best ~20-min average power


_COLOR_RANK = {"red": 3, "yellow": 2, "green": 1, "gray": 0}


def _compute_sport_adherence(target: dict | None, actual_activities: list[dict], sport: str) -> dict:
    """Compares a structured run_target/bike_target (written the day the advice was
    given) against what was actually logged that calendar day. See SPORT_TYPES for
    the sport->activityType grouping (shared with _training_load_by_sport)."""
    matching = [a for a in actual_activities if a.get("type") in SPORT_TYPES[sport]]
    any_activity = bool(actual_activities)

    if target is None:
        color = "yellow" if matching or any_activity else "gray"
        return {"color": color, "target": None, "actual_duration_min": None, "actual_avg_hr": None}

    if not matching:
        color = "yellow" if any_activity else "red"
        return {"color": color, "target": target, "actual_duration_min": None, "actual_avg_hr": None}

    total_duration = sum(a.get("duration_min") or 0 for a in matching)
    hr_values = [(a.get("duration_min") or 0, a.get("avg_hr")) for a in matching if a.get("avg_hr") is not None]
    weighted_hr = (
        round(sum(d * hr for d, hr in hr_values) / sum(d for d, hr in hr_values), 1)
        if hr_values and sum(d for d, hr in hr_values) > 0
        else None
    )

    duration_ratio = total_duration / target["duration_min"] if target.get("duration_min") else None
    hr_ok = weighted_hr is not None and (target["hr_min"] - 5) <= weighted_hr <= (target["hr_max"] + 5)
    duration_ok = duration_ratio is not None and 0.75 <= duration_ratio <= 1.35

    color = "green" if duration_ok and hr_ok else "yellow"
    return {
        "color": color,
        "target": target,
        "actual_duration_min": total_duration,
        "actual_avg_hr": weighted_hr,
    }


def _compute_adherence(target_entry: dict, actual_activities: list[dict]) -> dict:
    """Scores every sport in SPORT_TYPES, not just enabled ones — a sport disabled
    AFTER this day's advice was written should still show its historical adherence
    correctly, and a disabled sport with no target simply scores gray (rest/no
    target) same as it always has, so there's no need to filter by enabled_sports
    here at all."""
    advice = target_entry.get("advice", {})
    result = {}
    for sport, field in SPORT_FIELD.items():
        result[field] = _compute_sport_adherence(advice.get(f"{field}_target"), actual_activities, sport)
    day_color = max((s["color"] for s in result.values()), key=lambda c: _COLOR_RANK[c])
    result["day_color"] = day_color
    return result


def read_coach_log(user_id: int) -> list[dict]:
    """This user's own log of past advice, kept on the persisted coach-home
    volume — the Claude session itself is reset (`/clear`) after every run,
    so without this each day starts from zero with no memory of what was
    advised or how the user's ACWR/readiness trended over the past runs."""
    log_file = owner_log_file(user_id)
    if not os.path.exists(log_file):
        return []
    with open(log_file) as f:
        entries = json.load(f)
    cutoff = (NOW_LOCAL - timedelta(days=LOG_HISTORY_DAYS)).date().isoformat()
    return [e for e in entries if e.get("date", "") >= cutoff]


TIP_STRUCTURE = """TIP STRUCTURE — applies to run_tip, bike_tip, walk_tip, and to the weekly run_advice, bike_advice and walk_advice (only for sports listed in enabled_sports — see below). Every one of these fields MUST follow this exact structure, in {LANGUAGE}:

Write exactly two blocks separated by a single line break ("\\n"). No headers, no bullets, no bold.

BLOCK 1 — THE CALL. One short sentence (max ~15 words). State the decision first, before any reasoning. Two allowed forms, nothing else:
- Session advised: name the session type, duration, and intensity target, matching the run_target/bike_target/walk_target numbers exactly. Example shape: "Easy run today: 40 min at HR 130-148."
- No session advised: state that explicitly as a decision, optionally with the reason in three words. Example shape: "No second ride today — this morning covers it." Never skip Block 1 or replace it with reasoning when there is no session; "do nothing" is a call and must be stated as one.

BLOCK 2 — THE WHY, then at most ONE watch-out. Two sentences maximum.
- Sentence 1: the evidence for the call. Cite at most 3 concrete values from the data (dates, distances, HR, ACWR, HRV, zone percentages). Every value must come from the provided data — never invent numbers.
- Sentence 2 (optional): exactly one forward-looking caveat or execution cue, also grounded in a real number. If you have nothing important, omit it. Never add a second caveat.

HARD RULES:
- The order is always: decision -> evidence -> caveat. Never open with a fact or reasoning ("You haven't run yet...", "Your ACWR is..."); open with what to do.
- run_tip, bike_tip and walk_tip must be structurally parallel: same two-block shape, same sentence roles, so they read as one coach speaking.
- Do not repeat a number already used in Block 1 inside Block 2 unless it adds new meaning.
- Keep each full tip under 500 characters. Plain declarative sentences, no wordplay or idioms — the text must translate cleanly into any language."""

JSON_SCHEMA_DAILY = """{
  "status": "<1 sentence: sleep/resting HR/body battery/training readiness score>",
  "run_tip": "<follows TIP STRUCTURE above, only if \\"running\\" is in enabled_sports — otherwise omit/null>",
  "bike_tip": "<follows TIP STRUCTURE above, only if \\"cycling\\" is in enabled_sports — otherwise omit/null>",
  "walk_tip": "<follows TIP STRUCTURE above, only if \\"walking\\" is in enabled_sports — otherwise omit/null>",
  "run_target": "<{duration_min: int, hr_min: int, hr_max: int} or null if no run session advised today>",
  "bike_target": "<{duration_min: int, hr_min: int, hr_max: int} or null if no bike session advised today>",
  "walk_target": "<{duration_min: int, hr_min: int, hr_max: int} or null if no walk session advised today>",
  "tomorrow_run": "<1 tentative one-liner: workout type + duration + HR range for tomorrow's run, e.g. 'Likely an easy run, ~35 min, HR 130-148' — or 'Likely a rest day' if no run is probable. Null if genuinely too uncertain to say anything.>",
  "tomorrow_bike": "<1 tentative one-liner: workout type + duration + power range (from cycling_power_zones if available, otherwise HR range) for tomorrow's ride, e.g. 'Likely a tempo ride, ~45 min, 175-210W' — or 'Likely a rest day' if no ride is probable. Null if genuinely too uncertain to say anything.>",
  "tomorrow_walk": "<1 tentative one-liner: same shape as tomorrow_run but for tomorrow's walk, e.g. 'Likely an easy walk, ~30 min, HR 110-130' — or 'Likely a rest day' if no walk is probable. Null if genuinely too uncertain to say anything.>",
  "color": "green or yellow"
}"""

JSON_SCHEMA_WEEKLY = """{
  "performance": "<2-3 sentences overall weekly assessment, referencing the trend across recent_activities_14d, coach_log, vo2max, and intensity_distribution_7d/28d>",
  "recovery": "<2-3 sentences, explicitly mention the ACWR ratio and training_load_by_sport>",
  "run_advice": "<follows TIP STRUCTURE above, applied to the coming week's plan instead of just today, only if \\"running\\" is in enabled_sports — otherwise omit/null>",
  "bike_advice": "<follows TIP STRUCTURE above, applied to the coming week's plan instead of just today, only if \\"cycling\\" is in enabled_sports — otherwise omit/null>",
  "walk_advice": "<follows TIP STRUCTURE above, applied to the coming week's plan instead of just today, only if \\"walking\\" is in enabled_sports — otherwise omit/null>",
  "watch_point": "<1-2 sentences, or 'Nothing notable'>",
  "color": "green, orange or red"
}"""


def build_prompt(metrics: dict, weekly: bool, coach_log: list[dict], owner_id: int, enabled_sports: list[str]) -> str:
    metrics = {**metrics, "enabled_sports": enabled_sports}
    m = json.dumps(metrics, indent=2, ensure_ascii=False)
    log = json.dumps(coach_log, indent=2, ensure_ascii=False) if coach_log else "[]"
    disclaimer = (
        "No medical claims or diagnoses. For persistent issues, refer to a doctor "
        "or physiotherapist — avoid overly confident statements."
    )
    context = """Field explanations:
- training_readiness.score (0-100) and .level: Garmin's own readiness score for today.
  If "available": false or "data_stale": true is set: this data is missing or stale
  (more than 36 hours old) — don't rely on it; fall back on baseline_deviation, sleep and ACWR.
  training_readiness.factor_hrv / factor_sleep / factor_recovery_time / factor_acwr /
  factor_stress_history (0-100 each, higher = more favorable) are Garmin's own breakdown of
  what's driving the score — use them to explain WHY readiness is high or low, and to check
  whether a disagreement with baseline_deviation traces back to HRV itself or to a non-HRV
  factor (sleep, stress history, recovery time).
- training_status.acute_chronic_load_ratio (ACWR): <0.8 = too little load (undertrained),
  0.8-1.3 = healthy zone, 1.3-1.5 = caution (ramping up), >1.5 = ramping up fast — treat
  these as a load-ramp guardrail (how fast load is rising vs. your recent average), not as
  a direct injury prediction; the science behind the original "injury risk" framing has been
  formally challenged, so don't state it as established fact. This covers overall load
  (running + cycling combined) and is the PRIMARY figure for "should today involve hard
  training or not" — the body has one recovery system regardless of how many sports produced
  the load, so this combined number always takes priority over the per-sport figures below.
- training_load_by_sport.<running|cycling>.load_ramp_ratio: this week's training-load sum for
  that sport specifically, divided by its 4-week weekly average — same interpretation bands as
  ACWR above, but per sport (Garmin's own ACWR is combined). This is a SECONDARY, diagnostic
  signal, not a second recovery gate — use it only to explain WHICH sport is driving a change
  (e.g. "the combined ACWR is fine, but running specifically ramped up fast, so keep that one
  sport conservative even though cycling volume could handle more"), never to override or add
  to the combined ACWR's verdict on overall training load. Don't call this figure "ACWR" — it's
  a lighter, differently-weighted approximation using Garmin's per-activity load number.
  null means not enough load in the last 28 days to compute a meaningful ratio. Treat a very
  high ratio (e.g. >3) with caution if weekly_avg_last_28d is small (e.g. under ~30) — with a
  low, sparse baseline (that sport trained only occasionally) a single session this week can
  produce a misleadingly extreme ratio; describe it as "picked up this sport again" rather than
  a dramatic load spike in that case.
- training_status.status: Garmin's classification (e.g. PRODUCTIVE, PEAKING, OVERREACHING, RECOVERY).
- intensity_distribution_7d / intensity_distribution_28d: percentage of all heart-rate-zone
  time (across all activities in the window) spent low (zone 1-2, easy), mid (zone 3, "grey
  zone" — neither properly easy nor a real hard effort), or high (zone 4-5). Recreational
  endurance athletes generally do best with a polarized/pyramidal distribution: mostly easy
  volume (roughly 75-80%+ low), a small high-intensity portion, and minimal zone 3 — zone 3
  creep (easy days drifting into moderate effort) is a common, coachable mistake. If
  mid_zone3_pct is notably elevated (e.g. above ~20-25%) and there's been no explicit hard
  session logged, mention it as a pattern to watch, especially in the weekly review. If
  "available": false, not enough HR-zone data in the window yet.
- vo2max.running / vo2max.cycling: current estimated VO2max and the change over the last 28
  days (delta_28d), when available — a slow multi-week rise is a good sign of improving
  aerobic fitness, a drop of more than ~1-2 points can reflect detraining or incomplete
  recovery. Don't over-interpret single-day noise; this is more useful as a slow trend than
  a daily signal, so mention it mainly in the weekly review unless it moved sharply.
- lactate_threshold_running.threshold_hr_running: exact, measured heart rate threshold for running.
- lactate_threshold_running.threshold_pace_min_per_km: exact, measured threshold pace for running,
  ALREADY FORMATTED as "M:SS min/km" (e.g. "5:41 min/km") — reuse this notation verbatim,
  don't recalculate or interpret it as a decimal number.
- cycling_power_zones: PREFERRED source for cycling intensity targets when "available": true.
  estimated_ftp_watts is derived from the single best sustained (18+ min) power lap in the last
  90 days, using the standard "FTP ≈ 95% of best ~20-min power" rule — a real estimate from
  actual ride data, not a guess, but also not a dedicated FTP test, so phrase cycling advice in
  watts using these zones and mention once that the FTP is estimated from recent rides (not a
  formal test). zone1-6 are the standard % of FTP power zones (recovery/endurance/tempo/
  threshold/VO2max/anaerobic); zone6's upper bound is intentionally unbounded (null).
- cycling_hr_zones: fallback ONLY — use this for cycling intensity targets when
  cycling_power_zones is NOT available (e.g. not enough long laps yet). These are generic,
  calculated heart rate zones (Karvonen formula based on resting HR and measured max HR during
  cycling activities), less precise than a power-based estimate. Mention this fallback status
  when using it. Never mix watts and this HR zone in the same tip — pick one source per tip
  based on what's available.
- today.sleep_hours, today.sleep_score (Garmin's own 0-100 sleep quality score) and
  today.sleep_vs_prior_6d_avg_hours (difference vs. the average of the preceding 6 days, so
  not counting today itself): sleep is a FULL decision factor alongside readiness/ACWR, not
  just a status figure. A clear deficit (e.g. -1.5h or more) or a low sleep_score should
  noticeably temper the intensity/duration of the advice, even if body battery/readiness look
  fine on their own — a high body battery after little sleep mostly reflects current energy,
  not full physical recovery.
- today.activities_already_done_today: sport activities ALREADY completed since local midnight
  today (type, name, distance, duration in minutes, avg heart rate, calories, local_time —
  the clock time, e.g. "05:55", the activity started). If you mention time of day (morning/
  afternoon/evening) for a session, base it strictly on local_time — never assume or guess
  "this morning" just because it's the first/only session shown; check the actual clock time
  (roughly: before 12:00 = morning, 12:00-18:00 = afternoon, after 18:00 = evening). This
  applies equally when describing multiple sessions the same day (e.g. one at 05:55 and one at
  13:50 is "one this morning, one this afternoon", not "two rides this morning"). The user
  regularly trains the same sport twice in one day (e.g. a short morning ride + a longer
  evening ride) — "already done once" does NOT automatically mean "done for today". Assess
  per sport whether another session today is still reasonable:
  * If the completed session was short/light (e.g. <45 min, low avg HR relative to the
    threshold HR) and readiness/ACWR otherwise look good: a 2nd, additional session is fine —
    treat it as a normal training day with a possibly lighter 2nd session (e.g. a short
    endurance ride/run or recovery session), not as "extra on top of an already full day".
  * If the completed session was long/hard (e.g. >90 min, or avg HR close to/above the
    threshold HR), or there are already 2+ sessions of the same sport: advise against another
    session of that sport, give at most a short recovery tip, and explicitly mention what was
    already done (type/duration/distance).
  For a sport not done at all today: give normal advice. If the list is empty: give a
  suggestion for every enabled sport.
- enabled_sports: the sports this user actually wants advice for — a plain list containing
  any of "running", "cycling", "walking". Only write tip/target/tomorrow fields (run_*,
  bike_*, walk_*) for sports present in this list; for any sport NOT in this list, omit that
  sport's fields entirely (or set them null) and don't mention that sport anywhere in the
  response — treat it as if it doesn't exist for this user, not as a rest day for that sport.
- run_target / bike_target / walk_target: structured version of your run_tip/bike_tip/walk_tip
  advice, used to later check whether the plan was actually followed. Set to null if you are
  not advising a session for that sport today (e.g. rest day, or already covered under
  activities_already_done_today). Otherwise: {"duration_min": <int>, "hr_min": <int>,
  "hr_max": <int>} — duration_min is your intended session length in minutes, hr_min/hr_max
  is the target average-heart-rate RANGE for that session (not a hard ceiling — an average
  across the whole session), used for tracking adherence afterwards. Always fill this with a
  HR range even when bike_tip's headline intensity is given in watts (from
  cycling_power_zones) — convert/estimate the equivalent HR range for the target power zone so
  adherence can still be tracked from recorded heart rate. walk_target has no dedicated zone
  data (no lactate-threshold or power estimate for walking) — base its HR range on a simple
  easy-effort read (well below lactate_threshold_running.threshold_hr_running when available,
  otherwise a conservative low-zone estimate from resting/max HR seen in the data), since
  walking sessions are, by nature, easy-effort rather than a zone-targeted workout. These must
  be consistent with what you just wrote in run_tip/bike_tip/walk_tip, not a separate or
  vaguer suggestion — e.g. if run_tip says "40 minute easy run, HR 130-150", run_target must be
  {"duration_min": 40, "hr_min": 130, "hr_max": 150}.
- recent_activities_14d: full activity history of the last 14 days (date, local_time, type,
  duration, avg HR), NOT necessarily one entry per day — there can be, and often are, rest days with no
  entry at all in between. Use this to vary the workout type sensibly instead of suggesting the
  same generic endurance session every day — e.g. if there's been no intensity in several days
  and readiness/ACWR allow it, a tempo or interval session is appropriate; if there have already
  been 2+ hard sessions this week, favor endurance/recovery instead. Reference concrete
  sessions from this list when relevant (e.g. "after yesterday's 12km tempo run..."). Never
  state a count of consecutive days ("Nth day in a row") unless you have actually checked the
  `date` field of every entry and confirmed there is no gap — when in doubt, just describe the
  pattern qualitatively (e.g. "frequent cycling load this week") instead of inventing a number.
- schedule.is_long_day: true if the user has designated today's weekday (schedule.day_of_week)
  as a long-session day (user-configurable in settings, defaults to Saturday/Sunday). When true
  AND readiness/ACWR/sleep don't argue against it, favor a longer endurance session (duration
  meaningfully above your usual daily suggestion, e.g. 75+ min) over a shorter tempo/interval
  session for that sport — this is the user's own weekly-structure preference, not just another
  data signal, so don't override it with a short session unless recovery signals genuinely
  call for an easy/rest day instead. When false, plan normally (no bias toward or against length).
- coach_log: your own last 14 days of advice (JSON, oldest first). Use this to
  stay consistent (don't contradict yesterday's plan without reason) and to build an actual
  short-term plan across days (e.g. if you suggested an easy day yesterday, today can pick up
  intensity again, referencing that). If empty, this is one of the first runs — say so is not
  necessary, just give standalone advice.
- tomorrow_run / tomorrow_bike / tomorrow_walk (daily only): brief, tentative one-liners for
  tomorrow, one per enabled sport, reasoning forward from today's plan and recent load — e.g. if
  today is an easy/recovery day for a sport, tomorrow likely has room for intensity in that
  sport; if today includes a hard session, tomorrow is probably easier or a rest day for that
  sport. Each one-liner must name a workout type (easy/tempo/interval/rest, or just "easy walk"
  for walking) AND include a concrete duration + intensity range so the user can mentally
  prepare (e.g. "Likely an easy run, ~35 min, HR 130-148" or, for cycling, prefer watts over HR
  when cycling_power_zones is available: "Likely a tempo ride, ~45 min, 175-210W"). Keep the
  WORDING tentative ("likely", not a firm commitment) even though
  the numbers are concrete — tomorrow's actual advice will be generated fresh with tomorrow's
  real data (sleep, HRV, whether today's plan was actually followed), so this is same-day
  mental preparation, not a promise to be held to. If a rest day is likely for a sport, say so
  plainly (e.g. "Likely a rest day") rather than inventing a session. Set to null only if
  genuinely too uncertain to say anything useful (e.g. very early in the coach_log history).
- baseline_deviation.hrv_deviation_sd / resting_hr_deviation_sd: today's HRV/resting HR
  expressed as standard deviations from your 28-day rolling baseline. Roughly: within ±1 SD
  is normal day-to-day variation, beyond ±1 SD is a notable deviation worth mentioning,
  beyond ±2 SD is a strong signal (HRV notably below baseline or RHR notably above baseline
  both suggest incomplete recovery). IMPORTANT — relationship to training_readiness: HRV also
  feeds into Garmin's readiness score above, so these are two partially overlapping views of
  the same underlying recovery state, NOT two independent signals. Never cite them as separate
  pieces of evidence for the same conclusion — when they agree, lead with the explicit SD
  number as your cited evidence and mention the readiness score at most once as confirmation,
  without escalating the severity because "both" say so. When they clearly disagree (e.g.
  readiness MODERATE or better while hrv_deviation_sd is at or below -2 or
  resting_hr_deviation_sd at or above +2 — or readiness LOW while both deviations are within
  ±1 SD), do both of the following: (1) let the MORE CAUTIOUS of the two cap today's
  intensity — never use the more optimistic signal to argue a warning away; (2) name the
  disagreement explicitly in the tip so the user can see the signals diverged. One nuance: a
  single night's HRV is noisier than Garmin's multi-night smoothing, so a lone deviation
  between 1 and 2 SD against an otherwise good readiness score justifies caution in intensity,
  not automatically a full rest day. If "available": false, this data isn't available yet
  (needs at least 7 days of history)."""

    recovery_note = (
        "If running/cycling is discouraged today (low readiness, high ACWR, or training_status "
        "on RECOVERY/OVERREACHING): give a short walking suggestion instead (e.g. 20-30 min easy "
        "walk) as an active-recovery alternative — that's not a contradiction of resting, active "
        "recovery promotes blood flow without adding load. State explicitly that it's a recovery "
        "day, not a training day. If \"walking\" is itself in enabled_sports, put this suggestion "
        "in walk_tip/walk_target (that's what those fields are for) instead of folding it into "
        "run_tip or bike_tip."
    )

    schema = JSON_SCHEMA_WEEKLY if weekly else JSON_SCHEMA_DAILY
    role = (
        'You are an experienced fitness/recovery coach, similar to Garmin\'s own '
        '"Daily Suggested Workouts" feature but with more context and explanation.'
        if weekly
        else 'You are a fitness coach who, like Garmin\'s "Daily Suggested Workouts", gives '
        'concrete, actionable training advice for TODAY — not just "take it easy" or a bare '
        'HR range, but a specific workout type (endurance run, tempo, intervals, recovery, '
        'rest), with duration and intensity target, chosen based on recent training history '
        'and what was advised previously — not a generic template repeated every day.'
    )
    comparison_note = (
        "\n`last_7d_avg` = this week, `prev_7d_avg` = last week, `last_28d_avg` = 4-week "
        "baseline. Explicitly compare trends (improvement/decline), not just a snapshot.\n"
        if weekly
        else ""
    )

    return f"""{role} Always give advice for each enabled sport (see enabled_sports below) in its
own separate field, never mixed into one. Be evidence-based: every recommendation must reference
a specific number from the data below (e.g. "ACWR is 0.9" or "HRV is 1.3 SD below your
baseline"), not vague statements like "your body seems tired".

TRAINING PHILOSOPHY: the user's explicit goal is progressive improvement, not indefinite
maintenance — default to the more ambitious, load-building option whenever the safety signals
below allow it (e.g. prefer a longer duration or a harder intensity zone over the minimal
"safe" version, prefer adding a session over resting when nothing flags a problem). Injury
prevention is the only hard constraint, not general caution — don't hedge toward an easier
plan "just in case" when ACWR, baseline_deviation, sleep, and training_readiness are all
unremarkable. Treat these as the actual limits that should make you dial back or recommend
rest: ACWR above ~1.3-1.5 (see the ACWR guidance below), baseline_deviation showing HRV at or
below -2 SD or resting HR at or above +2 SD, a clear sleep deficit (-1.5h or more, or a low
sleep_score), or training_readiness/training_status flagging LOW/OVERREACHING. Outside of
those, push the plan forward rather than defaulting to the conservative choice.
{recovery_note}

Use this Garmin data (already pre-computed — no raw time series, don't use any numbers other
than what's given below):

{m}

Your own advice from the last {LOG_HISTORY_DAYS} days (coach_log, oldest first):

{log}

{context}
{comparison_note}
{TIP_STRUCTURE.format(LANGUAGE=LANGUAGE)}

Respond with valid JSON per this schema. Write all text values in {LANGUAGE}, EXCEPT the
"color" field — always keep that in English exactly as shown in the schema (green/yellow/
orange/red), regardless of {LANGUAGE}:
{schema}

{disclaimer}

Write ONLY the JSON (nothing else, no explanation, no markdown code block) to the file
{owner_output_file(owner_id)} using the {get_provider(PROVIDER)["write_tool_name"]} tool. Then give a brief confirmation in the chat."""


# Matched against English color-name substrings so this keeps working regardless
# of LANGUAGE — Claude is instructed to write the "color" value in English
# (see JSON_SCHEMA_*), only the advice text itself is translated.
COLOR_MAP = {"green": 0x2ECC71, "yellow": 0xF1C40F, "orange": 0xE67E22, "red": 0xE74C3C}


def _field(value: str | None) -> str:
    value = (value or "-").strip()
    return value[:1024] if value else "-"


def post_discord(embed: dict):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("No Discord webhook URL configured — set one on the dashboard's Settings page.")
    body = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord post failed: {resp.status}")


def _footer_text() -> str:
    return f"Garmin AI Coach — {get_provider(PROVIDER)['label']}"


def build_embed(advice: dict, weekly: bool) -> dict:
    color = COLOR_MAP.get(str(advice.get("color", "")).lower(), 0x95A5A6)
    today_str = NOW_LOCAL.strftime("%d-%m-%Y")

    if weekly:
        fields = [
            {"name": "📊 Performance", "value": _field(advice.get("performance")), "inline": False},
            {"name": "😴 Recovery", "value": _field(advice.get("recovery")), "inline": False},
        ]
        if advice.get("run_advice"):
            fields.append({"name": "🏃 Running", "value": _field(advice.get("run_advice")), "inline": False})
        if advice.get("bike_advice"):
            fields.append({"name": "🚴 Cycling", "value": _field(advice.get("bike_advice")), "inline": False})
        if advice.get("walk_advice"):
            fields.append({"name": "🚶 Walking", "value": _field(advice.get("walk_advice")), "inline": False})
        fields.append({"name": "⚠️ Watch point", "value": _field(advice.get("watch_point")), "inline": False})
        return {
            "title": f"🏃🚴 Weekly overview — {today_str}",
            "color": color,
            "fields": fields,
            "footer": {"text": _footer_text()},
        }
    fields = [{"name": "Status", "value": _field(advice.get("status")), "inline": False}]
    if advice.get("run_tip"):
        fields.append({"name": "🏃 Running", "value": _field(advice.get("run_tip")), "inline": False})
    if advice.get("bike_tip"):
        fields.append({"name": "🚴 Cycling", "value": _field(advice.get("bike_tip")), "inline": False})
    if advice.get("walk_tip"):
        fields.append({"name": "🚶 Walking", "value": _field(advice.get("walk_tip")), "inline": False})
    if advice.get("tomorrow_run") or advice.get("tomorrow_bike") or advice.get("tomorrow_walk"):
        preview_lines = []
        if advice.get("tomorrow_run"):
            preview_lines.append(f"🏃 {advice['tomorrow_run']}")
        if advice.get("tomorrow_bike"):
            preview_lines.append(f"🚴 {advice['tomorrow_bike']}")
        if advice.get("tomorrow_walk"):
            preview_lines.append(f"🚶 {advice['tomorrow_walk']}")
        fields.append({"name": "🔭 Tomorrow (preview)", "value": _field("\n".join(preview_lines)), "inline": False})
    return {
        "title": f"📅 Today — {today_str}",
        "color": color,
        "fields": fields,
        "footer": {"text": _footer_text()},
    }


def post_error_to_discord(error: Exception):
    embed = {
        "title": "⚠️ Garmin AI Coach — failed",
        "color": 0x95A5A6,
        "description": f"```{str(error)[:1900]}```",
        "footer": {"text": _footer_text()},
    }
    try:
        post_discord(embed)
    except Exception:
        pass  # if even the error message can't be sent, there's nothing more to do


