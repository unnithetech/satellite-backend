import os, time, math, requests, json, threading
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from sgp4.api import Satrec, jday
from fastapi import FastAPI

# ----------------- LOAD ENV -----------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 10))  # seconds between automatic updates

# ----------------- GROUND STATION -----------------
GS_LAT = math.radians(float(os.getenv("GS_LAT", "9.984780")))
GS_LON = math.radians(float(os.getenv("GS_LON", "76.477498")))
GS_ALT = float(os.getenv("GS_ALT", 0))  # meters
MIN_ELEV = float(os.getenv("MIN_ELEVATION", 10))  # degrees

EARTH_RADIUS = 6378.137  # km

# ----------------- SATELLITES CONFIG -----------------
# Each satellite: {"id": Supabase UUID, "name": str, "tle1": str, "tle2": str, "sat": Satrec}
SAT_INSTANCES = [
    {
        "id": os.getenv("SAT1_ID", "a03f7556-094b-44c8-991c-2f376de988d3"),
        "name": "Starlink-101",
        "tle1": "1 48251U 23001A   24016.50000000  .00000000  00000+0  00000-0 0  9991",
        "tle2": "2 48251  53.0000  0.0000 0000000  0.0000  0.0000 15.00000000000000"
    },
    {
        "id": os.getenv("SAT2_ID", "ee6942d7-75de-4830-9658-fa422fde0900"),
        "name": "Hubble Space Telescope",
        "tle1": "1 20580U 90037B   24016.50000000  .00000000  00000+0  00000-0 0  9991",
        "tle2": "2 20580  28.5000  0.0000 0000000  0.0000  0.0000 15.00000000000000"
    },
    {
        "id": os.getenv("SAT3_ID", "sim-sat-1-uuid-here"),
        "name": "ISS",
        "tle1": "1 25544U 98067A   24016.50000000  .00016717  00000+0  10270-3 0  9991",
        "tle2": "2 25544  51.6416  63.1454 0004985 120.8323 239.2876 15.49815327430823"
    }
]

# Initialize Satrec objects
for s in SAT_INSTANCES:
    s["sat"] = Satrec.twoline2rv(s["tle1"], s["tle2"])

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

# ----------------- MATH HELPERS -----------------
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

# ----------------- SATELLITE LOGIC -----------------
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

# ----------------- FASTAPI -----------------
app = FastAPI()

@app.get("/")
def root():
    return {"message": "Satellite tracker running!"}

@app.get("/update")
def manual_update():
    for s in SAT_INSTANCES:
        update_live_state(s["sat"], s["id"])
    return {"status": "updated"}

# ----------------- AUTO UPDATE LOOP -----------------
def auto_update_loop():
    while True:
        for s in SAT_INSTANCES:
            update_live_state(s["sat"], s["id"])
        time.sleep(UPDATE_INTERVAL)

@app.on_event("startup")
def startup_event():
    print("Generating orbit paths & passes for all satellites...")
    for s in SAT_INSTANCES:
        generate_orbit_path(s["sat"], s["id"])
        predict_passes(s["sat"], s["id"])
    print("Startup complete! Starting automatic updates every", UPDATE_INTERVAL, "seconds.")
    threading.Thread(target=auto_update_loop, daemon=True).start()

# ----------------- RUN (for local testing) -----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
