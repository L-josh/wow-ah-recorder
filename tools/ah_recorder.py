"""Records Blizzard's auction house snapshots for WoW Forever, about one an hour.

Usage: python -I tools/ah_recorder.py           save any snapshot not yet saved, then exit
       python -I tools/ah_recorder.py probe     list every auction house with a few prices, to find yours
       python -I tools/ah_recorder.py house N   record auction house N from now on

Blizzard republishes each auction house about hourly and keeps no history, so every new
response is saved as it came: data/ah-history/<house>-<kind>-<time>.json.gz. kind is
"auctions" (single items and gear) or "commodities" (stackables); time is Blizzard's
Last-Modified, in UTC. A check that finds nothing new saves nothing. What was done, and
anything that went wrong, goes to data/ah-history/recorder.log.

On this machine the Windows scheduled task "WoW AH recorder" runs the first form every 2
minutes, windowless (pythonw), while the user is logged in. Blizzard posts one snapshot an hour
at a time that creeps (x:25 on 24 Nov, x:26 on 25 Nov) and now and then skips one; checking
often saves each within 2 minutes, and a check that finds nothing new costs a token and two 304s.

Needs a Blizzard API client (free, https://develop.battle.net/access/clients), kept outside
the project in ~/.blizzard-api.json as {"client_id": "...", "client_secret": "..."}, or in the
environment as BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET.

A copy of this file also runs on GitHub Actions (private repo L-josh/wow-ah-recorder, folder
cloud-recorder here) so hours are kept while this PC is off; on this PC each run also fetches
what that copy saved and this PC missed, every half hour (ah_cloud_sync.py). Copy this file
into cloud-recorder/tools when it changes.

Blizzard has not documented the Forever endpoints. Players mapped them:
https://us.forums.blizzard.com/en/blizzard/t/wow-forever-api-what-works-so-far-plus-a-ts-client/60334
"""
import base64
import datetime
import email.utils
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "ah-history"
KEY_FILE = Path.home() / ".blizzard-api.json"
TOKEN_URL = "https://oauth.battle.net/token"
API = "https://us.api.blizzard.com"
NAMESPACE = "dynamic-forever-us"
# Any valid ruleset lists the same auction houses; the house alone picks the market.
RULESET = 1
# kind: what follows the house id in the address
KINDS = {"auctions": "", "commodities": "/commodities"}
# Shown by probe, to match a house against prices seen in game.
PROBE_ITEMS = {2592: "Wool Cloth", 4306: "Silk Cloth", 8170: "Rugged Leather"}
TIMEOUT = 120
RETRIES = 3
PAUSE = 2  # seconds before the first retry; doubles each time


def say(text):
    """To the screen, when there is one, and to the log."""
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    print(line)
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "recorder.log", "a", encoding="utf-8") as log:
        log.write(line + "\n")


def read_key():
    if os.environ.get("BLIZZARD_CLIENT_ID") and os.environ.get("BLIZZARD_CLIENT_SECRET"):
        return os.environ["BLIZZARD_CLIENT_ID"].strip(), os.environ["BLIZZARD_CLIENT_SECRET"].strip()
    try:
        key = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        client, secret = key["client_id"].strip(), key["client_secret"].strip()
    except (OSError, ValueError, KeyError, AttributeError):
        client = secret = ""
    if not client or not secret or "PASTE" in client + secret:
        raise SystemExit("No Blizzard API key yet: put the client ID and secret in %s" % KEY_FILE)
    return client, secret


def request(url, headers, data=None):
    """One exchange -> (status, headers, body). 304 and 404 are answers; a busy or failing server is retried."""
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT) as reply:
                body = reply.read()
                if reply.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return reply.status, reply.headers, body
        except urllib.error.HTTPError as error:
            if error.code in (304, 404):
                return error.code, error.headers, b""
            if (error.code != 429 and error.code < 500) or attempt == RETRIES:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == RETRIES:
                raise
        time.sleep(PAUSE * 2 ** attempt)


def token(key):
    basic = base64.b64encode(("%s:%s" % key).encode()).decode()
    try:
        _, _, body = request(TOKEN_URL, {"Authorization": "Basic " + basic,
                                         "Content-Type": "application/x-www-form-urlencoded"}, b"grant_type=client_credentials")
    except urllib.error.HTTPError as error:
        if error.code in (400, 401):
            raise SystemExit("Blizzard refused the client ID and secret in %s (HTTP %d)" % (KEY_FILE, error.code))
        raise
    return json.loads(body)["access_token"]


def get(path, bearer, since=None):
    headers = {"Authorization": "Bearer " + bearer, "Accept-Encoding": "gzip"}
    if since:
        headers["If-Modified-Since"] = since
    return request("%s%s?namespace=%s&locale=en_US" % (API, path, NAMESPACE), headers)


def load_state():
    try:
        return json.loads((DATA / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    part = DATA / "state.json.part"
    part.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(part, DATA / "state.json")


def record():
    state = load_state()
    house = state.get("house")
    if not house:
        raise SystemExit("No auction house chosen yet: run probe, then house N")
    bearer = token(read_key())
    seen = state.setdefault("seen", {})
    for kind, suffix in KINDS.items():
        last = seen.get(kind, {})
        status, headers, body = get("/data/wow/ruleset/%d/auctions/%d%s" % (RULESET, house, suffix), bearer, last.get("modified"))
        if status == 304:
            continue
        if status == 404:
            say("house %d %s: Blizzard has no such page (404)" % (house, kind))
            continue
        # Without a usable Last-Modified the same snapshot would come back every time: the content decides.
        digest = hashlib.sha1(body).hexdigest()
        if digest == last.get("sha1"):
            continue
        count = len(json.loads(body).get("auctions", []))
        modified = headers.get("Last-Modified")
        when = email.utils.parsedate_to_datetime(modified) if modified else datetime.datetime.now(datetime.timezone.utc)
        name = "%d-%s-%s.json.gz" % (house, kind, when.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        packed = gzip.compress(body, 6, mtime=0)
        part = DATA / (name + ".part")
        part.write_bytes(packed)
        os.replace(part, DATA / name)
        seen[kind] = {"modified": modified, "sha1": digest}
        save_state(state)
        say("saved %s: %d auctions, %d KB" % (name, count, len(packed) // 1024))


def lowest(auctions):
    """Cheapest price for one unit of each item id, in copper.

    A commodity carries unit_price. A single auction carries buyout for the whole stack,
    and none at all when it is bid-only.
    """
    low = {}
    for auction in auctions:
        price = auction.get("unit_price") or (auction.get("buyout") or 0) // max(auction.get("quantity") or 1, 1)
        item = auction["item"]["id"]
        if price and price < low.get(item, price + 1):
            low[item] = price
    return low


def money(copper):
    return "%dg%02ds%02dc" % (copper // 10000, copper // 100 % 100, copper % 100)


def probe():
    bearer = token(read_key())
    status, _, body = get("/data/wow/ruleset/%d/auctions/" % RULESET, bearer)
    if status != 200:
        raise SystemExit("Blizzard has no list of auction houses for ruleset %d (HTTP %d)" % (RULESET, status))
    print("%5s  %-26s %9s  %s" % ("house", "name", "listings", "  ".join("%-14s" % name for name in PROBE_ITEMS.values())))
    for house in sorted(json.loads(body).get("auctions", []), key=lambda entry: entry["id"]):
        name = house.get("name")
        if isinstance(name, dict):
            name = name.get("en_US")
        status, _, body = get("/data/wow/ruleset/%d/auctions/%d/commodities" % (RULESET, house["id"]), bearer)
        auctions = json.loads(body).get("auctions", []) if status == 200 else []
        low = lowest(auctions)
        print("%5d  %-26s %9d  %s" % (house["id"], name, len(auctions),
                                     "  ".join("%-14s" % (money(low[item]) if item in low else "none") for item in PROBE_ITEMS)))


def choose(house):
    state = load_state()
    if state.get("house") != house:
        state = {"house": house, "seen": {}}
        save_state(state)
    say("recording auction house %d" % house)


def cloud_sync():
    """Fetches snapshots the GitHub copy saved while this PC was off; ah_cloud_sync.py is not in
    the GitHub copy."""
    import importlib.util  # python -I leaves this folder off sys.path
    path = Path(__file__).with_name("ah_cloud_sync.py")
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location("ah_cloud_sync", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for line in module.sync_if_due(DATA):
        say(line)


def main(arguments):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        if not arguments:
            record()
            if not os.environ.get("GITHUB_ACTIONS"):
                cloud_sync()
        elif arguments == ["probe"]:
            probe()
        elif len(arguments) == 2 and arguments[0] == "house" and arguments[1].isdigit():
            choose(int(arguments[1]))
        else:
            print(__doc__)
            return 2
    except SystemExit as stop:
        say(str(stop))
        return 1
    except Exception as error:  # the scheduled run has no screen: the log is the only place a failure shows
        say("error: %s: %s" % (type(error).__name__, error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
