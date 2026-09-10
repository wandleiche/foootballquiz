#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Football quiz database updater.

Squads come from football-data.org.

Market values and portraits come from the published transfermarkt-datasets
file (CC0) rather than from scraping Transfermarkt, because Transfermarkt
blocks CI runner IP ranges. Live scraping is still attempted when the script
runs somewhere that can reach it, and TheSportsDB plus Wikipedia fill the
remaining photo gaps.

The report includes diagnostics that matter when data looks stale:
  - which season football-data.org is actually serving per league
  - whether live Transfermarkt was reachable
  - dataset hit counts, so a source going dark is visible
  - players whose Transfermarkt club disagrees with the API squad
"""

import re
import io
import requests
import json
import time
import os
from datetime import datetime
from urllib.parse import quote
from collections import defaultdict
import sys

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("[ERROR] beautifulsoup4 not installed. Run: pip install beautifulsoup4")
    sys.exit(1)

# ========================================
# CONFIGURATION
# ========================================

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
THESPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
TRANSFERMARKT_BASE = "https://www.transfermarkt.de"

# Published Transfermarkt dataset (dcaribou/transfermarkt-datasets, CC0).
# This is the primary source for market values and portrait URLs: it is a
# ready-made file meant for reuse, so nothing here talks to Transfermarkt
# itself - which matters because Transfermarkt blocks CI runner IP ranges.
TM_DATASET_URL = ("https://pub-e682421888d945d684bcae8890b0ec20.r2.dev"
                  "/data/players.csv.gz")

FOOTBALL_DATA_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

# Transfermarkt serves a reduced page without browser-like headers
TM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.transfermarkt.de/",
}

LEAGUES_TO_PROCESS = {
    "Bundesliga": 2002,
    "Serie A": 2019,
    "Ligue 1": 2015,
    "Premier League": 2021,
    "Primera Division": 2014,
}

DB_FILE = "football_quiz_complete.json"
IMAGE_DIR = "player_images"

# Words too generic to prove two club names refer to the same club
CLUB_STOPWORDS = {
    "fussball", "fußball", "club", "calcio", "football", "sport", "sportverein",
    "verein", "athletic", "atletico", "united", "city", "real", "deportivo",
}

# ========================================
# DATABASE I/O
# ========================================

def load_db(file_path):
    """Load existing flat player list. Returns dict keyed by player_id string."""
    if not os.path.exists(file_path):
        print("[INFO] No existing database, starting fresh.")
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)

        if isinstance(data, list):
            return {str(p['id']): p for p in data if 'id' in p}

        # Legacy nested format -> migrate
        if isinstance(data, dict) and 'leagues' in data:
            print("[INFO] Migrating legacy nested format...")
            players = {}
            for league_name, league_data in data['leagues'].items():
                for team in league_data.get('teams', []):
                    for player in team.get('squad', []):
                        pid = str(player.get('id', ''))
                        if pid:
                            player.setdefault('team_name', team.get('name', ''))
                            player.setdefault('league_name', league_name)
                            players[pid] = player
            return players
        return {}
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[ERROR] Cannot parse database: {e}")
        sys.exit(1)


def save_db(players_dict, file_path, quiet=False):
    players_list = sorted(
        players_dict.values(),
        key=lambda p: (p.get('league_name', ''), p.get('team_name', ''), p.get('name', ''))
    )
    # Write to a temp file first, then replace: a crash mid-write would
    # otherwise leave a truncated JSON that the app cannot decode.
    tmp_path = file_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(players_list, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)
    if not quiet:
        print(f"\n[INFO] Saved {len(players_list)} players to {file_path}")

# ========================================
# FOOTBALL-DATA.ORG
# ========================================

def get_competition_season(league_id):
    """
    Return a human-readable description of the season the API is serving.
    A stale season here explains relegated teams still showing up.
    """
    try:
        r = requests.get(f"{FOOTBALL_DATA_BASE_URL}/competitions/{league_id}",
                         headers=FOOTBALL_DATA_HEADERS, timeout=10)
        if r.status_code != 200:
            return f"unknown (HTTP {r.status_code})"
        season = r.json().get('currentSeason') or {}
        start = (season.get('startDate') or '?')[:10]
        end = (season.get('endDate') or '?')[:10]
        matchday = season.get('currentMatchday')
        return f"{start} .. {end} (Spieltag {matchday})"
    except Exception as e:
        return f"unknown ({e})"


def get_league_teams(league_id):
    url = f"{FOOTBALL_DATA_BASE_URL}/competitions/{league_id}/teams"
    try:
        r = requests.get(url, headers=FOOTBALL_DATA_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json().get('teams', [])
        print(f"[ERROR] League {league_id}: HTTP {r.status_code}")
        return []
    except Exception as e:
        print(f"[ERROR] League {league_id}: {e}")
        return []


def get_team_squad(team_id):
    url = f"{FOOTBALL_DATA_BASE_URL}/teams/{team_id}"
    try:
        r = requests.get(url, headers=FOOTBALL_DATA_HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                'id': team_id,
                'name': data.get('name'),
                'crest': data.get('crest'),
                'squad': data.get('squad', []),
            }
        print(f"[WARN] Team {team_id}: HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"[ERROR] Team {team_id}: {e}")
        return None

# ========================================
# TRANSFERMARKT
# ========================================

def format_market_value(euros):
    """
    Turn 18000000 into "18,00 Mio. €" - the German format the Swift model
    already parses, so no app change is needed.
    """
    try:
        v = float(euros)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}".replace('.', ',') + " Mio. €"
    return f"{v / 1_000:.0f} Tsd. €"


def load_tm_dataset():
    """
    Download the published Transfermarkt dataset and build lookup tables.

    Returns (by_tm_id, by_name). by_name only contains names that are unique
    in the dataset, so an ambiguous name never produces a wrong match.
    """
    import gzip, csv, collections
    try:
        print("[INFO] Lade Transfermarkt-Datensatz...")
        r = requests.get(TM_DATASET_URL, timeout=120)
        if r.status_code != 200:
            print(f"[WARN] Datensatz nicht verfuegbar (HTTP {r.status_code}).")
            return {}, {}
        with gzip.open(io.BytesIO(r.content), 'rt', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"[WARN] Datensatz konnte nicht geladen werden: {e}")
        return {}, {}

    by_id = {row['player_id']: row for row in rows if row.get('player_id')}
    counts = collections.Counter(normalize_name(r['name']) for r in rows)
    by_name = {normalize_name(r['name']): r
               for r in rows if counts[normalize_name(r['name'])] == 1}
    # name + date of birth stays unique even where the name alone repeats
    by_name_dob = {(normalize_name(r['name']), (r.get('date_of_birth') or '')[:10]): r
                   for r in rows if (r.get('date_of_birth') or '').strip()}

    print(f"[INFO] Datensatz: {len(rows)} Spieler "
          f"({len(by_name)} eindeutige Namen).")
    return by_id, by_name, by_name_dob


def normalize_name(name):
    return re.sub(r'[^a-z]', '', (name or '').lower())


def lookup_tm_dataset(player_name, tm_url, dob, by_id, by_name, by_name_dob):
    """
    Match in order of certainty:
      1. Transfermarkt id taken from tmUrl
      2. name + date of birth
      3. name, but only when it is unique in the dataset
    """
    m = re.search(r'/spieler/(\d+)', tm_url or '')
    if m:
        hit = by_id.get(m.group(1))
        if hit:
            return hit, 'ID'

    if dob:
        hit = by_name_dob.get((normalize_name(player_name), dob[:10]))
        if hit:
            return hit, 'DOB'

    hit = by_name.get(normalize_name(player_name))
    return (hit, 'NAME') if hit else (None, None)


def transfermarkt_reachable():
    """
    One probe before the main loop.

    Transfermarkt blocks datacenter IP ranges, so on a GitHub Actions runner
    every request fails. Without this check the script still walks all ~2600
    players, sleeping between each pointless attempt: about 100 wasted minutes
    per night, plus 2600 blocked requests aimed at someone else's server.
    """
    probe = f"{TRANSFERMARKT_BASE}/manuel-neuer/profil/spieler/17259"
    try:
        r = requests.get(probe, headers=TM_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[WARN] Transfermarkt nicht erreichbar (HTTP {r.status_code}).")
            return False
        if 'data-header' not in r.text:
            print("[WARN] Transfermarkt liefert unerwartete Seite (Bot-Schutz?).")
            return False
        print("[INFO] Transfermarkt erreichbar.")
        return True
    except Exception as e:
        print(f"[WARN] Transfermarkt nicht erreichbar: {e}")
        return False


def search_tm_url(player_name, team_name):
    """Search Transfermarkt for a player and return their profile URL."""
    url = f"{TRANSFERMARKT_BASE}/schnellsuche/ergebnis/schnellsuche?query={quote(player_name)}"
    try:
        r = requests.get(url, headers=TM_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')

        # Prefer the result whose club matches the API squad
        for row in soup.select('table.items tbody tr'):
            name_cell = row.select_one('td.hauptlink a')
            if not name_cell:
                continue
            href = name_cell.get('href', '')
            if '/profil/spieler/' not in href:
                continue
            club_cell = row.select_one('td.zentriert a[href*="/verein/"]')
            club_text = club_cell.get_text(strip=True) if club_cell else ''
            if clubs_match(team_name, club_text):
                return TRANSFERMARKT_BASE + href

        first = soup.select_one(
            'table.items tbody tr td.hauptlink a[href*="/profil/spieler/"]')
        if first:
            return TRANSFERMARKT_BASE + first.get('href', '')
    except Exception:
        pass
    return None


def significant_club_words(name):
    """Distinctive lowercase words from a club name, for loose comparison."""
    if not name:
        return set()
    cleaned = re.sub(r'[^\w\s]', ' ', name.lower())
    return {w for w in cleaned.split()
            if len(w) >= 4 and not w.isdigit() and w not in CLUB_STOPWORDS}


def clubs_match(name_a, name_b):
    """
    True if two club names plausibly refer to the same club.
    Loose on purpose: sources spell clubs differently
    ("1.FSV Mainz 05" vs "1. FSV Mainz 05").
    """
    a, b = significant_club_words(name_a), significant_club_words(name_b)
    if not a or not b:
        return True  # not enough signal to claim a mismatch
    return bool(a & b)


def parse_info_table(soup):
    """
    Transfermarkt's profile table is a flat run of label/value spans:
      span.info-table__content--regular = label, next --bold span = value.
    """
    out = {}
    spans = soup.select('div.info-table span.info-table__content')
    i = 0
    while i < len(spans) - 1:
        if 'regular' in ' '.join(spans[i].get('class') or []):
            nxt = spans[i + 1]
            if 'bold' in ' '.join(nxt.get('class') or []):
                label = spans[i].get_text(' ', strip=True).rstrip(':').strip()
                out[label] = nxt.get_text(' ', strip=True)
                i += 2
                continue
        i += 1
    return out


def scrape_transfermarkt(tm_url):
    """
    Scrape photo, market value, foot, age and current club.
    Returns a dict (possibly without 'photo_url') or None if the page failed.
    """
    if not tm_url:
        return None
    try:
        r = requests.get(tm_url, headers=TM_HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, 'html.parser')
        result = {}

        # --- PHOTO ---
        img = (soup.select_one('img.data-header__profile-image')
               or soup.select_one('div.data-header__profile img')
               or soup.find('img', src=re.compile(
                   r'transfermarkt\.(com|de|technology)/portrait')))
        if img:
            src = img.get('src') or img.get('data-src') or ''
            src = re.sub(r'/portrait/(small|medium|header)/', '/portrait/big/', src)
            if src and 'default.jpg' not in src and 'silhouette' not in src:
                result['photo_url'] = src

        # --- MARKET VALUE ---
        # The number is a bare text node; the unit sits in span.waehrung.
        mv_el = soup.select_one('a.data-header__market-value-wrapper')
        if mv_el:
            for p in mv_el.select('p'):   # drop "Letzte Änderung: ..."
                p.decompose()
            m = re.search(r'([\d.,]+)\s*(Mio\.|Tsd\.)?\s*€',
                          mv_el.get_text(' ', strip=True))
            if m:
                result['market_value'] = (f"{m.group(1)} {m.group(2)} €"
                                          if m.group(2) else f"{m.group(1)} €")

        # --- FOOT / AGE / CLUB ---
        info = parse_info_table(soup)

        foot_raw = (info.get('Fuß') or '').lower()
        if 'rechts' in foot_raw:
            result['foot'] = 'right'
        elif 'links' in foot_raw:
            result['foot'] = 'left'
        elif 'beid' in foot_raw:          # "beidfüßig"
            result['foot'] = 'both'

        age_m = re.search(r'\((\d{1,2})\)', info.get('Geb./Alter', ''))
        if age_m:
            result['age'] = int(age_m.group(1))

        club = info.get('Aktueller Verein')
        if club:
            result['current_club'] = club

        return result

    except Exception as e:
        print(f"      [TM] Scrape error: {e}")
        return None

# ========================================
# THESPORTSDB FALLBACK
# ========================================

def upgrade_url_resolution(url):
    """Rewrite known image URLs to their larger variant."""
    if not url:
        return url
    # Transfermarkt portraits: header/small/medium are 139x181, big is 300x390
    url = re.sub(r'/portrait/(header|small|medium)/', '/portrait/big/', url)
    url = url.replace('/preview/', '/').replace('/small/', '/')
    # Wikipedia thumbnails
    url = re.sub(r'/(\d+)px-([^/]+)$', r'/600px-\2', url)
    return url


def search_thesportsdb(player_name, dob, nationality):
    try:
        r = requests.get(
            f"{THESPORTSDB_BASE_URL}/searchplayers.php?p={quote(player_name)}",
            timeout=10)
        if r.status_code != 200:
            return None
        players = r.json().get('player') or []
        if not players:
            return None
    except Exception:
        return None

    nat_lower = nationality.lower() if nationality else None

    def best_url(p):
        for field in ('strCutout', 'strThumb'):
            if p.get(field):
                return upgrade_url_resolution(p[field])
        return None

    def score(p):
        u = best_url(p)
        p_dob = p.get('dateBorn')
        if not u or not dob or not p_dob or p_dob != dob:
            return None   # a matching date of birth is required
        is_soccer = p.get('strSport', '').lower() == 'soccer'
        nat_match = nat_lower and nat_lower in p.get('strNationality', '').lower()
        if is_soccer and nat_match:
            return (1, u, 'TSDB_TIER1')
        if is_soccer:
            return (2, u, 'TSDB_TIER2')
        return (3, u, 'TSDB_TIER3')

    results = [s for p in players for s in [score(p)] if s]
    if not results:
        return None
    best = min(results, key=lambda x: x[0])
    return {'url': best[1], 'match': best[2], 'source': 'thesportsdb'}

# ========================================
# WIKIPEDIA FALLBACK
# ========================================

def search_wikipedia(player_name):
    try:
        search_r = requests.get(WIKIPEDIA_API_URL, params={
            'action': 'query', 'list': 'search',
            'srsearch': f"{player_name} footballer",
            'format': 'json', 'srlimit': 3,
        }, timeout=10)
        if search_r.status_code != 200:
            return None
        results = search_r.json().get('query', {}).get('search', [])
        if not results:
            return None

        img_r = requests.get(WIKIPEDIA_API_URL, params={
            'action': 'query', 'titles': results[0]['title'],
            'prop': 'pageimages', 'format': 'json',
            'pithumbsize': 600, 'pilicense': 'any',
        }, timeout=10)
        if img_r.status_code != 200:
            return None

        for page in img_r.json().get('query', {}).get('pages', {}).values():
            source = page.get('thumbnail', {}).get('source')
            if source:
                return {'url': upgrade_url_resolution(source),
                        'match': 'WIKIPEDIA', 'source': 'wikipedia'}
    except Exception:
        pass
    return None

# ========================================
# PHOTO DOWNLOAD
# ========================================

def download_photo(url, save_path):
    try:
        headers = TM_HEADERS if 'transfermarkt' in url else {}
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            with open(save_path, 'wb') as f:
                f.write(r.content)
            return True
        return False
    except Exception:
        return False

# ========================================
# FALLBACK HELPER
# ========================================

def try_fallbacks(flat, player_name, dob, nationality, old, player_id,
                  stats, missing_photos, league_name, team_name):
    """Try TheSportsDB then Wikipedia. Keeps an existing photo if there is one."""
    if old.get('photoUrl') and old.get('hasPhoto'):
        flat['hasPhoto'] = True
        flat['photoUrl'] = old['photoUrl']
        flat['photoSource'] = old.get('photoSource')
        flat['photo_path'] = old.get('photo_path')
        stats['existing_photos'] += 1
        return flat

    result = search_thesportsdb(player_name, dob, nationality)
    if not result:
        time.sleep(0.3)
        result = search_wikipedia(player_name)

    if result:
        safe = re.sub(r'[^\w\-]', '_', player_name)
        file_path = os.path.join(IMAGE_DIR, f"{safe}_{player_id}.png")
        if download_photo(result['url'], file_path):
            flat['hasPhoto'] = True
            flat['photoUrl'] = result['url']
            flat['photoSource'] = result['source']
            flat['photo_path'] = file_path
            stats['new_photos'] += 1
            stats['fallback_photos'] += 1
            print(f"      -> OK fallback: {result['match']}")
            return flat
        missing_photos[league_name].append(f"{player_name} ({team_name}) - download error")
    else:
        missing_photos[league_name].append(f"{player_name} ({team_name})")
        print(f"      -> no photo found")

    flat['hasPhoto'] = False
    flat['photoUrl'] = None
    return flat

# ========================================
# MAIN UPDATE LOGIC
# ========================================

def compare_and_update():
    old_db = load_db(DB_FILE)
    os.makedirs(IMAGE_DIR, exist_ok=True)

    new_db = {}
    stats = defaultdict(int)
    missing_photos = defaultdict(list)
    transfers = defaultdict(lambda: {'in': [], 'out': []})
    club_mismatches = []
    seasons = {}

    # Checked once. When Transfermarkt is blocked (CI runners are), all TM work
    # is skipped and existing marketValue/tmUrl/foot/age are carried over from
    # the previous run instead of being lost.
    # Primary enrichment source: works from any IP, so this is what actually
    # keeps market values and portraits populated in CI.
    ds_by_id, ds_by_name, ds_by_name_dob = load_tm_dataset()

    tm_ok = transfermarkt_reachable()
    if not tm_ok:
        print("[INFO] Live-Transfermarkt uebersprungen (gesperrt). "
              "Marktwerte/Fotos kommen aus dem Datensatz.")

    print(f"\n[INFO] Processing {len(LEAGUES_TO_PROCESS)} leagues...")

    for league_name, league_id in LEAGUES_TO_PROCESS.items():
        seasons[league_name] = get_competition_season(league_id)
        print(f"\n{'='*6} {league_name} {'='*6}")
        print(f"  API-Saison: {seasons[league_name]}")

        teams = get_league_teams(league_id)
        old_league_ids = {pid for pid, p in old_db.items()
                          if p.get('league_name') == league_name}
        new_league_ids = set()

        for team_info in teams:
            team_id = team_info['id']
            team_name = team_info['name']
            print(f"\n  [TEAM] {team_name}")

            squad_data = get_team_squad(team_id)
            if not squad_data:
                time.sleep(5)
                continue

            crest = squad_data.get('crest')
            squad = squad_data.get('squad', [])

            for player in squad:
                player_id = str(player['id'])
                player_name = player.get('name', '')
                dob = player.get('dateOfBirth')
                nationality = player.get('nationality')

                new_league_ids.add(player_id)
                stats['total_players'] += 1

                is_new = player_id not in old_db
                old = old_db.get(player_id, {})

                if is_new:
                    transfers[league_name]['in'].append(f"{player_name} -> {team_name}")
                    stats['transfers_in'] += 1

                # Transfermarkt URL: cached in the DB, searched only when missing
                tm_url = old.get('tmUrl')
                if not tm_url and tm_ok:
                    print(f"    ? {player_name}: searching Transfermarkt...")
                    tm_url = search_tm_url(player_name, team_name)
                    if tm_url:
                        print(f"      -> {tm_url}")
                    time.sleep(1)

                flat = {
                    'id': player['id'],
                    'name': player_name or old.get('name') or 'Unbekannt',
                    # Never emit null here: the Swift model treats these as
                    # non-optional strings, so a null would make the record
                    # undecodable in the app.
                    'position': (player.get('position')
                                 or old.get('position') or 'Unbekannt'),
                    'nationality': nationality or old.get('nationality'),
                    'team_name': team_name or 'Unbekannt',
                    'league_name': league_name,
                    # Stored so the dataset join has a second exact key besides
                    # the Transfermarkt id. Name alone is ambiguous for players
                    # like "Vitinha" or "Thiago".
                    'dateOfBirth': dob or old.get('dateOfBirth'),
                    'team_logo_url': crest,
                    'tmUrl': tm_url,
                    'age': old.get('age'),
                    'jerseyNumber': player.get('shirtNumber') or old.get('jerseyNumber'),
                    'marketValue': old.get('marketValue'),
                    'foot': old.get('foot'),
                }

                # --- DATASET ENRICHMENT (before any live scraping) ---
                ds_row, how = lookup_tm_dataset(
                    player_name, tm_url, dob,
                    ds_by_id, ds_by_name, ds_by_name_dob)
                if ds_row:
                    stats[f'ds_match_{how.lower()}'] += 1

                    mv = format_market_value(ds_row.get('market_value_in_eur'))
                    if mv:
                        flat['marketValue'] = mv
                        stats['ds_market_values'] += 1

                    if ds_row.get('foot'):
                        flat['foot'] = ds_row['foot'].strip().lower() or None

                    dob_ds = (ds_row.get('date_of_birth') or '')[:10]
                    if dob_ds:
                        try:
                            born = datetime.strptime(dob_ds, '%Y-%m-%d')
                            today = datetime.now()
                            flat['age'] = (today.year - born.year
                                           - ((today.month, today.day)
                                              < (born.month, born.day)))
                        except ValueError:
                            pass

                    # Keep the profile link so future runs join on the id
                    if not flat.get('tmUrl') and ds_row.get('url'):
                        flat['tmUrl'] = ds_row['url']
                        tm_url = ds_row['url']

                    # Portrait: the dataset ships /portrait/header/ at 139x181,
                    # too small for the quiz, so upgrade it to /portrait/big/.
                    ds_img = (ds_row.get('image_url') or '').strip()
                    if ds_img:
                        big = upgrade_url_resolution(ds_img)
                        if big != old.get('photoUrl'):
                            flat['hasPhoto'] = True
                            flat['photoUrl'] = big
                            flat['photoSource'] = 'transfermarkt-dataset'
                            stats['ds_photos'] += 1
                        else:
                            flat['hasPhoto'] = True
                            flat['photoUrl'] = big
                            flat['photoSource'] = old.get('photoSource') or 'transfermarkt-dataset'

                old_url = flat.get('photoUrl') or old.get('photoUrl')
                had_photo = bool(old_url) and bool(flat.get('hasPhoto') or old.get('hasPhoto'))

                if tm_url and tm_ok:
                    print(f"    * {player_name}: Transfermarkt...")
                    tm = scrape_transfermarkt(tm_url)

                    if tm:
                        # Refresh details on every run, they change over time
                        if tm.get('market_value'):
                            flat['marketValue'] = tm['market_value']
                        if tm.get('foot'):
                            flat['foot'] = tm['foot']
                        if tm.get('age'):
                            flat['age'] = tm['age']

                        # Squad staleness check: does TM agree on the club?
                        tm_club = tm.get('current_club')
                        if tm_club and not clubs_match(team_name, tm_club):
                            club_mismatches.append(
                                f"{player_name}: API={team_name} / TM={tm_club}")
                            stats['club_mismatches'] += 1
                            print(f"      -> WARN club mismatch: TM says {tm_club}")

                        new_url = tm.get('photo_url')
                        if new_url and new_url != old_url:
                            safe = re.sub(r'[^\w\-]', '_', player_name)
                            file_path = os.path.join(IMAGE_DIR, f"{safe}_{player_id}.jpg")
                            if download_photo(new_url, file_path):
                                flat['hasPhoto'] = True
                                flat['photoUrl'] = new_url
                                flat['photoSource'] = 'transfermarkt'
                                flat['photo_path'] = file_path
                                stats['new_photos'] += 1
                                stats['tm_photos'] += 1
                                print(f"      -> OK photo updated")
                            else:
                                flat['hasPhoto'] = had_photo
                                flat['photoUrl'] = old_url
                                flat['photoSource'] = old.get('photoSource')
                                flat['photo_path'] = old.get('photo_path')
                                print(f"      -> download failed, keeping old photo")
                        else:
                            flat['hasPhoto'] = had_photo
                            flat['photoUrl'] = old_url
                            flat['photoSource'] = old.get('photoSource')
                            flat['photo_path'] = old.get('photo_path')
                            if had_photo:
                                stats['existing_photos'] += 1
                                print(f"      -> details refreshed, photo unchanged")
                            else:
                                flat = try_fallbacks(
                                    flat, player_name, dob, nationality, old,
                                    player_id, stats, missing_photos,
                                    league_name, team_name)
                    else:
                        print(f"      -> TM page failed, trying fallbacks...")
                        flat = try_fallbacks(flat, player_name, dob, nationality, old,
                                             player_id, stats, missing_photos,
                                             league_name, team_name)
                else:
                    reason = "TM gesperrt" if tm_url else "keine TM-URL"
                    if not had_photo:
                        print(f"    ! {player_name}: {reason}, Fallbacks...")
                    flat = try_fallbacks(flat, player_name, dob, nationality, old,
                                         player_id, stats, missing_photos,
                                         league_name, team_name)

                new_db[player_id] = flat

                # Only pause when this player actually caused outbound requests.
                # Players that already have a photo and are skipping Transfermarkt
                # hit no external service at all, so sleeping on them just burns
                # runtime (about 34 minutes across a full run).
                if (tm_url and tm_ok) or not had_photo:
                    time.sleep(1)

            # Checkpoint after every team. The run takes hours, so a crash or
            # timeout must not throw away everything done so far.
            # Merged with old_db so a partial file never shrinks the database
            # the app downloads; the final save uses new_db alone, which is
            # what actually drops departed players.
            save_db({**old_db, **new_db}, DB_FILE, quiet=True)
            print(f"  [OK] {len(squad)} players processed "
                  f"({len(new_db)} total, checkpoint saved)")
            time.sleep(4)

        # Players who were in this league before but are not in any current squad
        for pid in old_league_ids - new_league_ids:
            left = old_db.get(pid, {})
            transfers[league_name]['out'].append(
                f"{left.get('name', pid)} <- {left.get('team_name', '?')}")
            stats['transfers_out'] += 1

    save_db(new_db, DB_FILE)
    generate_report(stats, missing_photos, transfers, club_mismatches, seasons, tm_ok)

# ========================================
# REPORT
# ========================================

def generate_report(stats, missing, transfers, club_mismatches, seasons, tm_ok=True):
    total = stats['total_players']
    print("\n" + "=" * 70)
    print(f"ABSCHLUSSBERICHT  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)

    print(f"\nTransfermarkt-Datensatz (CC0):")
    print(f"  Treffer via TM-ID:      {stats['ds_match_id']}")
    print(f"  Treffer via Name+Geb.:  {stats['ds_match_dob']}")
    print(f"  Treffer via Name:       {stats['ds_match_name']}")
    print(f"  Marktwerte gesetzt:     {stats['ds_market_values']}")
    print(f"  Portraits gesetzt:      {stats['ds_photos']}")
    print(f"\nLive-Transfermarkt: {'erreichbar' if tm_ok else 'gesperrt (uebersprungen)'}")

    print("\nAPI-Saison pro Liga (erklaert abgestiegene Teams):")
    for league, season in seasons.items():
        print(f"  {league:20} {season}")

    if not total:
        print("\nWARN: Keine Spieler verarbeitet.")
        return

    print(f"\nSpieler gesamt:           {total}")
    print(f"Fotos vorhanden (alt):    {stats['existing_photos']}")
    print(f"Fotos neu/aktualisiert:   {stats['new_photos']}")
    print(f"  davon Transfermarkt:    {stats['tm_photos']}")
    print(f"  davon Fallback:         {stats['fallback_photos']}")
    print(f"\nNeuzugaenge:              {stats['transfers_in']}")
    print(f"Abgaenge:                 {stats['transfers_out']}")

    for league, tx in transfers.items():
        if tx['in'] or tx['out']:
            print(f"\n  {league}:")
            if tx['in']:
                names = ', '.join(n.split('->')[0].strip() for n in tx['in'][:5])
                print(f"    + {len(tx['in'])} neu: {names}...")
            if tx['out']:
                names = ', '.join(n.split('<-')[0].strip() for n in tx['out'][:5])
                print(f"    - {len(tx['out'])} weg: {names}...")

    if club_mismatches:
        print(f"\nVEREINS-ABWEICHUNGEN: {len(club_mismatches)}")
        print("  (football-data.org listet den Spieler noch im Kader,")
        print("   Transfermarkt nennt einen anderen Verein -> API-Kader veraltet)")
        for m in club_mismatches[:30]:
            print(f"    - {m}")
        if len(club_mismatches) > 30:
            print(f"    ... und {len(club_mismatches) - 30} weitere")

    total_missing = sum(len(v) for v in missing.values())
    if total_missing:
        print(f"\nSpieler ohne Foto: {total_missing}")
        for league, players in missing.items():
            if players:
                print(f"  {league} ({len(players)}):")
                for p in players[:10]:
                    print(f"    - {p}")

    print("\n" + "=" * 70)
    print("Fertig.")

# ========================================
# ENTRY POINT
# ========================================

if __name__ == "__main__":
    CI_RUN = os.environ.get("CI") == "true"

    if not FOOTBALL_DATA_API_KEY:
        print("[ERROR] FOOTBALL_DATA_API_KEY not set.")
        sys.exit(1)

    print("=" * 70)
    print("FUSSBALL DATENBANK UPDATE (mit Transfermarkt)")
    print("=" * 70)

    if CI_RUN:
        print("CI mode - running automatically...")
        compare_and_update()
    else:
        ans = input("Start full update? (ja/nein): ")
        if ans.lower() in ('ja', 'j', 'yes', 'y'):
            compare_and_update()
        else:
            print("Aborted.")
