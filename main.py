import os, time, math, requests, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from sgp4.api import Satrec, jday

# ----------------- LOAD ENV -----------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SAT_ID = os.getenv("SATELLITE_ID")

GS_LAT = math.radians(float(os.getenv("GS_LAT")))
GS_LON = math.radians(float(os.getenv("GS_LON")))
GS_ALT = float(os.getenv("GS_ALT"))
MIN_ELEV = float(os.getenv("MIN_ELEVATION"))

EARTH_RADIUS = 6378.137  # km

# ----------------- TLE (ISS example) -----------------
TLE1 = "1 25544U 98067A   24016.50000000  .00016717  00000+0  10270-3 0  9991"
TLE2 = "2 25544  51.6416  63.1454 0004985 120.8323 239.2876 15.49815327430823"

sat = Satrec.twoline2rv(TLE1, TLE2)

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

# ----------------- LIVE STATE -----------------
def update_live_state():
    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day,
                  now.hour, now.minute, now.second)

    e, r, v = sat.sgp4(jd, fr)
    if e != 0:
        return

    lat, lon, alt = eci_to_latlon(r)
    speed = math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)

    supabase_insert("satellite_state", {
        "satellite_id": SAT_ID,
        "latitude": lat,
        "longitude": lon,
        "altitude_km": alt,
        "velocity_kms": speed,
        "timestamp": now.isoformat()
    })

# ----------------- ORBIT PATH (30 MIN) -----------------
def generate_orbit_path(minutes=30):
    supabase_delete("orbit_path", "satellite_id", SAT_ID)
    start = datetime.now(timezone.utc)

    for i in range(0, minutes * 60, 30):
        t = start + timedelta(seconds=i)
        jd, fr = jday(t.year, t.month, t.day,
                      t.hour, t.minute, t.second)

        e, r, _ = sat.sgp4(jd, fr)
        if e != 0:
            continue

        lat, lon, alt = eci_to_latlon(r)
        supabase_insert("orbit_path", {
            "satellite_id": SAT_ID,
            "latitude": lat,
            "longitude": lon,
            "altitude_km": alt,
            "timestamp": t.isoformat()
        })

# ----------------- PASS PREDICTION -----------------
def predict_passes(hours=24):
    supabase_delete("passes", "satellite_id", SAT_ID)

    t = datetime.now(timezone.utc)
    end = t + timedelta(hours=hours)

    in_pass = False
    aos = None
    max_el = 0

    while t < end:
        jd, fr = jday(t.year, t.month, t.day,
                      t.hour, t.minute, t.second)

        e, r, _ = sat.sgp4(jd, fr)
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
                "satellite_id": SAT_ID,
                "aos": aos.isoformat(),
                "los": t.isoformat(),
                "max_elevation_deg": max_el,
                "duration_sec": int((t - aos).total_seconds())
            })
            in_pass = False

        t += timedelta(seconds=20)

# ----------------- STARTUP -----------------
print("Generating orbit path & passes...")
generate_orbit_path()
predict_passes()

last_refresh = datetime.now(timezone.utc)

print("Starting live tracking...")
while True:
    update_live_state()

    if datetime.now(timezone.utc) - last_refresh > timedelta(hours=6):
        print("Refreshing orbit & passes...")
        generate_orbit_path()
        predict_passes()
        last_refresh = datetime.now(timezone.utc)

    time.sleep(5)