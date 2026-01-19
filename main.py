import os, math, requests, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI
from sgp4.api import Satrec, jday

# ----------------- LOAD ENV -----------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

GS_LAT = math.radians(float(os.getenv("GS_LAT")))
GS_LON = math.radians(float(os.getenv("GS_LON")))
GS_ALT = float(os.getenv("GS_ALT"))
MIN_ELEV = float(os.getenv("MIN_ELEVATION"))

EARTH_RADIUS = 6378.137  # km

# ----------------- SATELLITE LIST -----------------
SATELLITES = [
    {
        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "name": "ISS (ZARYA)",
        "norad_id": "25544",
        "tle1": "1 25544U 98067A   24016.50000000  .00016717  00000+0  10270-3 0  9991",
        "tle2": "2 25544  51.6416  63.1454 0004985 120.8323 239.2876 15.49815327430823"
    },
    {
        "id": "a03f7556-094b-44c8-991c-2f376de988d3",
        "name": "Starlink-101",
        "norad_id": "48251",
        "tle1": "1 48251U 21006A   24016.50000000  .00000250  00000+0  12345-4 0  9991",
        "tle2": "2 48251  53.0000 120.0000 0001000  90.0000 270.0000 15.00000000000000"
    },
    {
        "id": "ee6942d7-75de-4830-9658-fa422fde0900",
        "name": "Hubble Space Telescope",
        "norad_id": "20580",
        "tle1": "1 20580U 90037B   24016.50000000  .00000900  00000+0  56789-3 0  9991",
        "tle2": "2 20580  28.4700 345.0000 0002000 180.0000 360.0000 15.00000000000000"
    }
]

# ----------------- SUPABASE HELPERS -----------------
def supabase_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    r = requests.post(url, json=[data], headers=headers)
    if not r.ok:
        print(f"[ERROR] Insert {table}: {r.status_code} {r.text}")

def supabase_delete(table, column, value):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{column}=eq.{value}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    r = requests.delete(url, headers=headers)
    if not r.ok:
        print(f"[ERROR] Delete {table}: {r.status_code} {r.text}")

# ----------------- MATH -----------------
def eci_to_latlon(r):
    x, y, z = r
    lon = math.atan2(y, x)
    lat = math.atan2(z, math.sqrt(x*x + y*y))
    alt = math.sqrt(x*x + y*y + z*z) - EARTH_RADIUS
    return math.degrees(lat), math.degrees(lon), alt

def elevation_angle(r):
    x, y, z = r

    xg = (EARTH_RADIUS + GS_ALT) * math.cos(GS_LAT) * math.cos(GS_LON)
    yg = (EARTH_RADIUS + GS_ALT) * math.cos(GS_LAT) * math.sin(GS_LON)
    zg = (EARTH_RADIUS + GS_ALT) * math.sin(GS_LAT)

    rx, ry, rz = x - xg, y - yg, z - zg
    range_norm = math.sqrt(rx*rx + ry*ry + rz*rz)

    dot = rx*xg + ry*yg + rz*zg
    elev = math.asin(dot / (range_norm * EARTH_RADIUS))
    return math.degrees(elev)

# ----------------- TRACKING -----------------
def update_live_state(sat_obj, sat_id):
    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day,
                  now.hour, now.minute, now.second)

    e, r, v = sat_obj.sgp4(jd, fr)
    if e != 0:
        return

    lat, lon, alt = eci_to_latlon(r)
    speed = math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)

    supabase_insert("satellite_state", {
        "satellite_id": sat_id,
        "latitude": lat,
        "longitude": lon,
        "altitude_km": alt,
        "velocity_kms": speed,
        "timestamp": now.isoformat()
    })

def generate_orbit_path(sat_obj, sat_id, minutes=30):
    supabase_delete("orbit_path", "satellite_id", sat_id)
    start = datetime.now(timezone.utc)
    for i in range(0, minutes * 60, 30):
        t = start + timedelta(seconds=i)
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second)
        e, r, _ = sat_obj.sgp4(jd, fr)
        if e != 0:
            continue
        lat, lon, alt = eci_to_latlon(r)
        supabase_insert("orbit_path", {
            "satellite_id": sat_id,
            "latitude": lat,
            "longitude": lon,
            "altitude_km": alt,
            "timestamp": t.isoformat()
        })

def predict_passes(sat_obj, sat_id, hours=24):
    supabase_delete("passes", "satellite_id", sat_id)
    t = datetime.now(timezone.utc)
    end = t + timedelta(hours=hours)
    in_pass = False
    aos = None
    max_el = 0

    while t < end:
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second)
        e, r, _ = sat_obj.sgp4(jd, fr)
        if e != 0:
            t += timedelta(seconds=20)
            continue

        el = elevation_angle(r)

        if el > MIN_ELEV and not in_pass:
            aos = t
            max_el = el
            in_pass = True
        elif el > MIN_ELEV:
            max_el = max(max_el, el)
        elif el <= MIN_ELEV and in_pass:
            supabase_insert("passes", {
                "satellite_id": sat_id,
                "aos": aos.isoformat(),
                "los": t.isoformat(),
                "max_elevation_deg": max_el,
                "duration_sec": int((t - aos).total_seconds())
            })
            in_pass = False
        t += timedelta(seconds=20)

# ----------------- INITIALIZE SATELLITE OBJECTS -----------------
SAT_INSTANCES = []
for s in SATELLITES:
    sat_obj = Satrec.twoline2rv(s["tle1"], s["tle2"])
    SAT_INSTANCES.append({"sat": sat_obj, "id": s["id"], "name": s["name"]})

# ----------------- FASTAPI -----------------
app = FastAPI(title="Satellite Tracker")

@app.on_event("startup")
def startup_event():
    print("Generating orbit paths & passes for all satellites...")
    for s in SAT_INSTANCES:
        generate_orbit_path(s["sat"], s["id"])
        predict_passes(s["sat"], s["id"])
    print("Startup complete!")

@app.get("/update")
def update_all():
    for s in SAT_INSTANCES:
        update_live_state(s["sat"], s["id"])
    return {"status": "updated", "time": datetime.now(timezone.utc).isoformat()}

@app.get("/satellites")
def list_satellites():
    return [{"id": s["id"], "name": s["name"]} for s in SAT_INSTANCES]
