import argparse
import math
import os
import time

import requests

OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSBDB_URL = "https://api.adsbdb.com/v0"


def haversine(lat1, lon1, lat2, lon2):
    """Straight-line distance between two points, in km (ignores altitude)."""
    radius = 6371.0 #todo create interface get this is value from a input (10,100) 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


class Aircraft:
    """State of one aircraft in a single radar reading (raw OpenSky vector)."""

    def __init__(self, state):
        self.icao24 = state[0]
        self.callsign = (state[1] or "").strip()
        self.country = state[2]
        self.lon = state[5]
        self.lat = state[6]
        self.altitude = state[7]        # meters (barometric)
        self.on_ground = state[8]
        self.velocity = state[9]        # m/s
        self.heading = state[10]        # degrees from north
        self.vertical_rate = state[11]  # m/s: positive climbing, negative descending

        self.distance = None
        self.flight = None
        self.route = None
        self.model = None

    @property
    def has_position(self):
        return self.lat is not None and self.lon is not None

    def calculate_distance(self, ref_lat, ref_lon):
        self.distance = haversine(ref_lat, ref_lon, self.lat, self.lon)

    def vertical_indicator(self):
        if self.on_ground:
            return "="
        climb = self.vertical_rate or 0
        if climb > 0.5:
            return "^"
        if climb < -0.5:
            return "v"
        return "-"

    def is_nearby(self, near_km):
        return (
            near_km is not None
            and self.distance is not None
            and self.distance <= near_km
        )

    def table_row(self, near_km=None):
        callsign = (self.callsign or self.icao24)[:8]
        flight = (self.flight or "-")[:7]
        route = (self.route or "-")[:10]
        model = (self.model or "-")[:9]
        altitude = f"{self.altitude:.0f}m" if self.altitude is not None else "-"
        speed = (
            f"{self.velocity * 3.6:.0f}km/h" if self.velocity is not None else "-"
        )
        distance = (
            f"{self.distance:>7.0f}km" if self.distance is not None else f"{'-':>9}"
        )
        marker = "★" if self.is_nearby(near_km) else ""
        return (
            f"{callsign:<9}{flight:<8}{route:<11}{model:<10}"
            f"{altitude:>7}{speed:>9}{self.vertical_indicator():>3}{distance}{marker:>5}"
        )


class RouteService:
    """Queries adsbdb (flight route and model/registration), with caching."""

    def __init__(self):
        self.route_cache = {}
        self.aircraft_cache = {}
        self.delay = 0.0  # seconds to wait before each new request

    def route_for(self, callsign):
        if not callsign:
            return None
        if callsign in self.route_cache:
            return self.route_cache[callsign]

        data = None
        try:
            if self.delay:
                time.sleep(self.delay)
            r = requests.get(f"{ADSBDB_URL}/callsign/{callsign}", timeout=8)
            if r.status_code == 200:
                route = r.json()["response"]["flightroute"]
                data = {
                    "flight": route.get("callsign_iata") or callsign,
                    "origin": route["origin"]["iata_code"],
                    "destination": route["destination"]["iata_code"],
                }
        except (requests.RequestException, KeyError, ValueError):
            data = None

        self.route_cache[callsign] = data
        return data

    def aircraft_info(self, icao24):
        if icao24 in self.aircraft_cache:
            return self.aircraft_cache[icao24]

        data = None
        try:
            if self.delay:
                time.sleep(self.delay)
            r = requests.get(f"{ADSBDB_URL}/aircraft/{icao24}", timeout=8)
            if r.status_code == 200:
                aircraft = r.json()["response"]["aircraft"]
                data = {
                    "registration": aircraft.get("registration", "?"),
                    "model": aircraft.get("type", "?"),
                }
        except (requests.RequestException, KeyError, ValueError):
            data = None

        self.aircraft_cache[icao24] = data
        return data


class Radar:
    """Full cycle: fetch positions, compute distances, enrich and display."""

    def __init__(self, bbox, ref_lat, ref_lon, interval=15, max_rows=15, near_km=3.0):
        self.bbox = bbox
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.interval = interval
        self.max_rows = max_rows
        self.near_km = near_km
        self.route_service = RouteService()
        self._first_reading = True

    def fetch_aircraft(self):
        try:
            response = requests.get(OPENSKY_URL, params=self.bbox, timeout=15)
        except requests.RequestException as err:
            print(f"[error] Could not connect to OpenSky: {err}")
            return []

        if response.status_code == 429:
            print("[warning] OpenSky rate limit reached — please wait a moment.")
            return []
        if response.status_code != 200:
            print(f"[warning] OpenSky returned status {response.status_code}")
            return []

        try:
            states = response.json().get("states") or []
        except ValueError:
            print("[warning] OpenSky response was not valid JSON")
            return []

        aircraft_list = []
        for s in states:
            try:
                aircraft_list.append(Aircraft(s))
            except (IndexError, TypeError):
                continue  # malformed state vector — skip this aircraft

        return [a for a in aircraft_list if a.has_position]

    def enrich(self, aircraft_list):
        # On the first cycle almost nothing is cached yet — space out new
        # requests a little so we don't trip adsbdb's per-minute limit.
        self.route_service.delay = 0.15 if self._first_reading else 0.0

        for aircraft in aircraft_list:
            route = self.route_service.route_for(aircraft.callsign)
            info = self.route_service.aircraft_info(aircraft.icao24)
            if route:
                aircraft.flight = route["flight"]
                aircraft.route = f"{route['origin']}>{route['destination']}"
            if info:
                aircraft.model = info["model"]

        self._first_reading = False

    def display(self, aircraft_list):
        os.system("cls" if os.name == "nt" else "clear")
        print(
            f"RADAR  —  {time.strftime('%H:%M:%S')}  —  {len(aircraft_list)} aircraft in zone\n"
        )

        header = (
            f"{'CALLSIGN':<9}{'FLIGHT':<8}{'ROUTE':<11}{'MODEL':<10}"
            f"{'ALT':>7}{'SPD':>9}{'':>3}{'DIST':>9}{'NEAR':>5}"
        )
        print(header)
        print("-" * len(header))
        for aircraft in aircraft_list[: self.max_rows]:
            print(aircraft.table_row(self.near_km))

    def cycle(self):
        aircraft_list = self.fetch_aircraft()
        for aircraft in aircraft_list:
            aircraft.calculate_distance(self.ref_lat, self.ref_lon)
        aircraft_list.sort(key=lambda a: a.distance)

        self.enrich(aircraft_list[: self.max_rows])
        self.display(aircraft_list)

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception as err:  # never let the demo crash mid-run
                print(f"[unexpected error] {err}")
            time.sleep(self.interval)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Real-time aircraft radar (OpenSky + adsbdb)"
    )
    parser.add_argument("--lamin", type=float, default=40.5, help="minimum latitude")
    parser.add_argument("--lomin", type=float, default=-9.5, help="minimum longitude")
    parser.add_argument("--lamax", type=float, default=42.2, help="maximum latitude")
    parser.add_argument("--lomax", type=float, default=-7.0, help="maximum longitude")
    parser.add_argument(
        "--ref-lat", type=float, default=41.15, help="reference point latitude"
    )
    parser.add_argument(
        "--ref-lon", type=float, default=-8.35, help="reference point longitude"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="seconds between updates (anonymous OpenSky won't return fresher data than 10s)",
    )
    parser.add_argument(
        "--rows", type=int, default=15, help="maximum number of aircraft shown"
    )
    parser.add_argument(
        "--near-km",
        type=float,
        default=3.0,
        help="flag aircraft within this many km of the reference point with a ★",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.interval < 10:
        print(
            "[warning] with anonymous OpenSky access, data doesn't refresh faster "
            "than every 10s — an --interval below that won't help.\n"
        )

    bbox = {
        "lamin": args.lamin,
        "lomin": args.lomin,
        "lamax": args.lamax,
        "lomax": args.lomax,
    }
    radar = Radar(
        bbox,
        args.ref_lat,
        args.ref_lon,
        interval=args.interval,
        max_rows=args.rows,
        near_km=args.near_km,
    )

    try:
        radar.run()
    except KeyboardInterrupt:
        print("\nRadar shut down.")


if __name__ == "__main__":
    main()