#!/usr/bin/env python3
"""Garmin AI coach: queries InfluxDB, aggregates metrics, asks Claude Code for
advice, posts the result to Discord. Run daily; on Sundays does a full weekly
review instead of the short daily check-in."""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

INFLUXDB_URL = os.environ["INFLUXDB_URL"]  # e.g. http://172.17.0.1:8187
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "GarminStats")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "Europe/Amsterdam"))
NOW_LOCAL = datetime.now(LOCAL_TZ)
IS_WEEKLY = NOW_LOCAL.weekday() == 6  # Sunday, in lokale tijd
STALE_AFTER_HOURS = 36
OUTPUT_FILE = "/app/output/advies.json"


def local_midnight_utc(days_ago: int = 0) -> datetime:
    """Middernacht in lokale tijdzone, N dagen geleden, omgerekend naar UTC."""
    midnight_local = NOW_LOCAL.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return midnight_local.astimezone(timezone.utc)


def influx_query(q: str) -> list[dict]:
    params = urllib.parse.urlencode({"db": INFLUXDB_DB, "q": q})
    with urllib.request.urlopen(f"{INFLUXDB_URL}/query?{params}", timeout=15) as resp:
        data = json.load(resp)
    result = data.get("results", [{}])[0]
    if "error" in result:
        raise RuntimeError(f"InfluxDB query error: {result['error']} (query: {q})")
    series = result.get("series")
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def avg(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def latest(measurement: str) -> dict:
    """Meest recente datapunt, met een staleness-vlag zodat de prompt weet of de
    data nog actueel genoeg is om op te vertrouwen."""
    rows = influx_query(f'SELECT * FROM "{measurement}" ORDER BY time DESC LIMIT 1')
    if not rows:
        return {"available": False}
    row = rows[0]
    ts = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    row["_stale"] = age_hours > STALE_AFTER_HOURS
    row["_age_hours"] = round(age_hours, 1)
    return row


def daily_stats_window(days_back: int, window_days: int) -> list[dict]:
    end = local_midnight_utc(days_back)
    start = local_midnight_utc(days_back + window_days)
    q = (
        f'SELECT restingHeartRate, totalSteps, stressPercentage, '
        f'bodyBatteryHighestValue, bodyBatteryLowestValue, sleepingSeconds '
        f'FROM "DailyStats" WHERE time >= \'{start.isoformat()}\' AND time < \'{end.isoformat()}\' '
        f'ORDER BY time DESC'
    )
    return influx_query(q)


def build_metrics() -> dict:
    today = daily_stats_window(0, 1)
    last_7d = daily_stats_window(0, 7)
    prev_7d = daily_stats_window(7, 7)
    last_28d = daily_stats_window(0, 28)

    today_sleep_h = round(today[0]["sleepingSeconds"] / 3600, 1) if today and today[0].get("sleepingSeconds") else None
    # sleep_vs_7d_avg vergelijkt tegen de 6 dagen VOOR vandaag, niet inclusief vandaag zelf,
    # anders wordt de afwijking structureel gedempt door vandaag mee te tellen in het gemiddelde.
    prior_6d = daily_stats_window(1, 6)
    prior_6d_sleep_avg = avg(prior_6d, "sleepingSeconds")

    return {
        "today": {
            "resting_hr": today[0].get("restingHeartRate") if today else None,
            "steps": today[0].get("totalSteps") if today else None,
            "body_battery_high": today[0].get("bodyBatteryHighestValue") if today else None,
            "body_battery_low": today[0].get("bodyBatteryLowestValue") if today else None,
            "sleep_hours": today_sleep_h,
            "sleep_vs_prior_6d_avg_hours": round(today_sleep_h - prior_6d_sleep_avg / 3600, 1)
            if today_sleep_h is not None and prior_6d_sleep_avg
            else None,
            "activities_already_done_today": _activities_today(),
        },
        "last_7d_avg": {
            "resting_hr": avg(last_7d, "restingHeartRate"),
            "steps": avg(last_7d, "totalSteps"),
            "stress_pct": avg(last_7d, "stressPercentage"),
            "sleep_hours": round(avg(last_7d, "sleepingSeconds") / 3600, 1)
            if avg(last_7d, "sleepingSeconds")
            else None,
            "days_with_data": len(last_7d),
        },
        "prev_7d_avg": {
            "resting_hr": avg(prev_7d, "restingHeartRate"),
            "steps": avg(prev_7d, "totalSteps"),
            "stress_pct": avg(prev_7d, "stressPercentage"),
        },
        "last_28d_avg": {
            "resting_hr": avg(last_28d, "restingHeartRate"),
            "steps": avg(last_28d, "totalSteps"),
        },
        "training_readiness": _training_readiness(),
        "training_status": _training_status(),
        "lactate_threshold_running": _lactate_threshold(),
        "cycling_hr_zones": _cycling_hr_zones(today, last_28d),
    }


def _activities_today() -> list[dict]:
    start = local_midnight_utc(0)
    rows = influx_query(
        f'SELECT activityType, activityName, distance, elapsedDuration, averageHR, calories '
        f'FROM "ActivitySummary" WHERE time >= \'{start.isoformat()}\''
    )
    result = []
    seen = set()
    for r in rows:
        activity_type = r.get("activityType")
        if not activity_type or activity_type == "No Activity":
            continue
        key = (activity_type, r.get("activityName"), r.get("distance"))
        if key in seen:  # garmin-fetch-data kan een activiteit dubbel wegschrijven bij re-sync
            continue
        seen.add(key)
        result.append(
            {
                "type": activity_type,
                "name": r.get("activityName"),
                "distance_km": round(r["distance"] / 1000, 1) if r.get("distance") else None,
                "duration_min": round(r["elapsedDuration"] / 60) if r.get("elapsedDuration") else None,
                "avg_hr": r.get("averageHR"),
                "calories": r.get("calories"),
            }
        )
    return result


def _training_readiness() -> dict:
    r = latest("TrainingReadiness")
    if not r.get("available", True):
        return r
    return {
        "score": r.get("score"),
        "level": r.get("level"),
        "acute_load": r.get("acuteLoad"),
        "recovery_time_hours": round(r["recoveryTime"] / 60, 1) if r.get("recoveryTime") else None,
        "sleep_score": r.get("sleepScore"),
        "data_stale": r.get("_stale", False),
    }


def _training_status() -> dict:
    r = latest("TrainingStatus")
    if not r.get("available", True):
        return r
    return {
        "status": r.get("trainingStatusFeedbackPhrase"),
        "acute_chronic_load_ratio": r.get("dailyAcuteChronicWorkloadRatio"),
        "acute_load": r.get("dailyTrainingLoadAcute"),
        "chronic_load": r.get("dailyTrainingLoadChronic"),
        "data_stale": r.get("_stale", False),
    }


def _format_pace(decimal_min_per_km: float) -> str:
    total_seconds = round(decimal_min_per_km * 60)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _lactate_threshold() -> dict:
    r = latest("LactateThreshold")
    if not r.get("available", True):
        return r
    # Garmin's lactateThresholdSpeed-endpoint geeft de waarde als seconden/meter (pace),
    # niet als m/s snelheid — geverifieerd tegen een realistisch drempeltempo (~5-6 min/km).
    sec_per_m = r.get("SpeedThreshold_RUNNING")
    pace_decimal = sec_per_m * 1000 / 60 if sec_per_m else None
    return {
        "threshold_hr_running": r.get("HeartRateThreshold_RUNNING"),
        "threshold_pace_min_per_km": f"{_format_pace(pace_decimal)} min/km" if pace_decimal else None,
        "data_stale": r.get("_stale", False),
    }


def _cycling_hr_zones(today: list[dict], last_28d: list[dict]) -> dict:
    # Geen fietsspecifieke lactaatdrempel beschikbaar via Garmin's API (endpoint bestaat
    # technisch, geeft geen data terug voor dit account) — bereken globale HR-zones met
    # de Karvonen-formule i.p.v. een echte FTP/drempeltest.
    resting_hr = (today[0].get("restingHeartRate") if today else None) or avg(last_28d, "restingHeartRate")
    # Alleen fiets-activiteiten voor max HR — hardlopen geeft doorgaans 5-10 bpm hogere piekwaarden
    # en zou de cycling-zones vertekenen.
    max_hr_rows = influx_query(
        "SELECT max(maxHR) FROM ActivitySummary WHERE time > now() - 90d "
        "AND (activityType = 'road_biking' OR activityType = 'indoor_cycling' OR activityType = 'mountain_biking')"
    )
    max_hr = max_hr_rows[0].get("max") if max_hr_rows else None
    if not resting_hr or not max_hr:
        return {"available": False}
    reserve = max_hr - resting_hr
    return {
        "available": True,
        "resting_hr": resting_hr,
        "max_hr_measured_90d_cycling": max_hr,
        "zone2_endurance_bpm": [round(resting_hr + reserve * 0.6), round(resting_hr + reserve * 0.7)],
        "zone3_tempo_bpm": [round(resting_hr + reserve * 0.7), round(resting_hr + reserve * 0.8)],
        "zone4_threshold_bpm": [round(resting_hr + reserve * 0.8), round(resting_hr + reserve * 0.9)],
        "zone5_vo2max_bpm": [round(resting_hr + reserve * 0.9), max_hr],
    }


JSON_SCHEMA_DAILY = """{
  "status": "<1 zin: slaap/rustpols/body battery/training readiness score>",
  "run_tip": "<1-2 zinnen hardloopadvies>",
  "bike_tip": "<1-2 zinnen fietsadvies>",
  "kleur": "groen of geel"
}"""

JSON_SCHEMA_WEEKLY = """{
  "performance": "<2-3 zinnen algemene weekbeoordeling>",
  "recovery": "<2-3 zinnen, noem expliciet de ACWR-ratio>",
  "run_advies": "<2-3 zinnen hardloopadvies komende week>",
  "bike_advies": "<2-3 zinnen fietsadvies komende week>",
  "aandachtspunt": "<1-2 zinnen, of 'Geen bijzonderheden'>",
  "kleur": "groen, oranje of rood"
}"""


def build_prompt(metrics: dict, weekly: bool) -> str:
    m = json.dumps(metrics, indent=2, ensure_ascii=False)
    disclaimer = (
        "Geen medische claims of diagnoses. Bij aanhoudende afwijkingen: "
        "verwijs naar arts/fysiotherapeut, geen stellige uitspraken."
    )
    context = """Uitleg van de velden:
- training_readiness.score (0-100) en .level: Garmin's eigen paraatheid-score voor vandaag.
  Als "available": false of "data_stale": true staat: deze data ontbreekt of is verouderd
  (meer dan 36 uur oud) — vertrouw er dan niet op, val terug op slaap/rustpols/ACWR.
- training_status.acute_chronic_load_ratio (ACWR): <0.8 = te weinig belasting (ondertraind),
  0.8-1.3 = gezonde zone, 1.3-1.5 = let op (opbouwend risico), >1.5 = verhoogd blessurerisico
  (te snel opgebouwd). Dit geldt voor de algehele belasting (hardlopen + fietsen samen).
- training_status.status: Garmin's kwalificatie (bv. PRODUCTIVE, PEAKING, OVERREACHING, RECOVERY).
- lactate_threshold_running.threshold_hr_running: exacte, gemeten hartslag-drempel voor hardlopen.
- lactate_threshold_running.threshold_pace_min_per_km: exact, gemeten drempeltempo hardlopen,
  AL GEFORMATTEERD als "M:SS min/km" (bv. "5:41 min/km") — neem deze notatie letterlijk over,
  reken niet zelf om of interpreteer niet als decimaal getal.
- cycling_hr_zones: GEEN gemeten drempel beschikbaar (Garmin levert dit niet voor fietsen op dit
  account) — dit zijn algemene, berekende hartslagzones (Karvonen-formule op basis van rustpols en
  gemeten max HR tijdens fietsactiviteiten), minder precies dan de running-drempel. Vermeld dit
  bij het fietsadvies.
- today.sleep_hours en today.sleep_vs_prior_6d_avg_hours (verschil t.o.v. het gemiddelde van de
  voorgaande 6 dagen, dus zonder vandaag zelf mee te tellen): slaap is een VOLWAARDIGE
  beslissingsfactor naast readiness/ACWR, niet alleen een statuscijfer. Een duidelijk tekort
  (bv. -1,5u of meer) moet de intensiteit/duur van het advies merkbaar temperen, ook als body
  battery/readiness op zichzelf goed ogen — een hoge body battery na weinig slaap zegt vooral
  iets over energie op dit moment, niet over volledig fysiek herstel.
- today.activities_already_done_today: sport-activiteiten die AL zijn uitgevoerd sinds lokale
  middernacht vandaag (type, naam, afstand, duur in minuten, gem. hartslag, calorieën). De
  gebruiker fietst regelmatig 2x op een dag (bv. een korte ochtendrit + een langere avondrit) —
  "al 1x gedaan" betekent dus NIET automatisch "klaar voor vandaag". Beoordeel per sport of een
  volgende sessie vandaag nog verantwoord is:
  * Was de al-gedane sessie kort/licht (bv. <45 min, lage gem. HR t.o.v. de threshold-HR) en is
    readiness/ACWR verder goed: een 2e, aanvullende sessie kan prima — behandel het dan als een
    normale trainingsdag met eventueel een lichtere 2e sessie (bv. een korte duurrit/duurloop of
    hersteltraining), niet als "extra bovenop een al volle dag".
  * Was de al-gedane sessie lang/zwaar (bv. >90 min, of gem. HR dicht bij/boven de threshold-HR),
    of staan er al 2+ sessies van dezelfde sport: raad een nieuwe sessie van diezelfde sport af,
    geef hooguit een korte hersteltip, en noem expliciet wat er al gedaan is (type/duur/afstand).
  Voor de sport die nog helemaal niet gedaan is vandaag: geef gewoon normaal advies. Is de lijst
  leeg: geef voor beide sporten een voorstel."""

    recovery_note = (
        "Als hardlopen/fietsen vandaag afgeraden wordt (lage readiness, hoge ACWR, of "
        "training_status op RECOVERY/OVERREACHING): geef dan een kort wandeladvies (bv. 20-30 min "
        "rustig wandelen) als actief-herstel-alternatief — dat is geen tegenstelling met "
        "herstellen, actieve rust bevordert doorbloeding zonder extra belasting. Zeg er expliciet "
        "bij dat het een hersteldag is, geen trainingsdag."
    )

    schema = JSON_SCHEMA_WEEKLY if weekly else JSON_SCHEMA_DAILY
    role = (
        'Je bent een ervaren fitness/hersteltrainer, vergelijkbaar met Garmin\'s eigen '
        '"Daily Suggested Workouts"-feature maar met meer context en uitleg.'
        if weekly
        else 'Je bent een fitness-coach die net als Garmin\'s "Daily Suggested Workouts" een '
        'concreet, uitvoerbaar trainingsadvies voor VANDAAG geeft — niet alleen "rustig aan" maar '
        'een specifiek workout-voorstel met type, duur en intensiteit-doel.'
    )
    comparison_note = (
        "\n`last_7d_avg` = deze week, `prev_7d_avg` = vorige week, `last_28d_avg` = 4-weken-baseline. "
        "Vergelijk expliciet trends (verbetering/verslechtering), niet alleen een snapshot.\n"
        if weekly
        else ""
    )

    return f"""{role} Geef hardloop- en fietsadvies altijd apart, nooit gemengd in één veld.
{recovery_note}

Gebruik deze Garmin-data (al vooraf berekend — geen ruwe tijdreeksen, gebruik geen andere
getallen dan hieronder gegeven):

{m}

{context}
{comparison_note}
Antwoord met geldige JSON volgens dit schema (Nederlandse tekst in de waarden):
{schema}

{disclaimer}

Schrijf UITSLUITEND de JSON (niets anders, geen toelichting, geen markdown-codeblok) weg naar
het bestand {OUTPUT_FILE} met de Write-tool. Geef daarna een korte bevestiging in de chat."""


def call_claude(prompt: str) -> dict:
    """Stuurt de prompt naar de permanente 'coach' tmux-sessie i.p.v. een verse
    `claude -p`-subprocess te starten — dat laatste triggert elke keer een
    kostbare cache-write (systeemprompt/tools opnieuw), wat disproportioneel
    veel van het sessiequotum verbruikt bij een koude/verse container-start.

    Claude schrijft het antwoord zelf naar OUTPUT_FILE (via de Write-tool) i.p.v.
    dat we het uit de tmux-scherm-tekst proberen te scrapen — schermtekst-parsing
    bleek fragiel (race conditions met tussentijdse 'thinking'-frames, line-wraps
    die JSON-strings braken, markers die te vroeg/laat gezien werden)."""
    import session_ask

    session_ask.ask_and_wait_for_file(prompt, OUTPUT_FILE)
    with open(OUTPUT_FILE) as f:
        response_text = f.read().strip()
    os.remove(OUTPUT_FILE)

    if response_text.startswith("```"):
        response_text = response_text.strip("`")
        if response_text.startswith("json"):
            response_text = response_text[4:]
        response_text = response_text.strip()
    return json.loads(response_text)


COLOR_MAP = {"groen": 0x2ECC71, "geel": 0xF1C40F, "oranje": 0xE67E22, "rood": 0xE74C3C}


def _field(value: str | None) -> str:
    value = (value or "-").strip()
    return value[:1024] if value else "-"


def post_discord(embed: dict):
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


def build_embed(advice: dict, weekly: bool) -> dict:
    color = COLOR_MAP.get(str(advice.get("kleur", "")).lower(), 0x95A5A6)
    today_str = NOW_LOCAL.strftime("%d-%m-%Y")

    if weekly:
        return {
            "title": f"🏃🚴 Week overzicht — {today_str}",
            "color": color,
            "fields": [
                {"name": "📊 Performance", "value": _field(advice.get("performance")), "inline": False},
                {"name": "😴 Recovery", "value": _field(advice.get("recovery")), "inline": False},
                {"name": "🏃 Hardlopen", "value": _field(advice.get("run_advies")), "inline": False},
                {"name": "🚴 Fietsen", "value": _field(advice.get("bike_advies")), "inline": False},
                {"name": "⚠️ Aandachtspunt", "value": _field(advice.get("aandachtspunt")), "inline": False},
            ],
            "footer": {"text": "Garmin AI Coach — Claude"},
        }
    return {
        "title": f"📅 Vandaag — {today_str}",
        "color": color,
        "fields": [
            {"name": "Status", "value": _field(advice.get("status")), "inline": False},
            {"name": "🏃 Hardlopen", "value": _field(advice.get("run_tip")), "inline": False},
            {"name": "🚴 Fietsen", "value": _field(advice.get("bike_tip")), "inline": False},
        ],
        "footer": {"text": "Garmin AI Coach — Claude"},
    }


def post_error_to_discord(error: Exception):
    embed = {
        "title": "⚠️ Garmin AI Coach — mislukt",
        "color": 0x95A5A6,
        "description": f"```{str(error)[:1900]}```",
        "footer": {"text": "Garmin AI Coach — Claude"},
    }
    try:
        post_discord(embed)
    except Exception:
        pass  # als zelfs de foutmelding niet verstuurd kan worden, is er niets meer te doen


def main():
    metrics = build_metrics()
    prompt = build_prompt(metrics, IS_WEEKLY)
    advice = call_claude(prompt)
    embed = build_embed(advice, IS_WEEKLY)
    post_discord(embed)
    print(f"Done ({'weekly' if IS_WEEKLY else 'daily'}):", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        post_error_to_discord(e)
        sys.exit(1)
