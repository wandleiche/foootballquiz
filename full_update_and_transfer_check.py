#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Football quiz database updater.

Squads come from football-data.org. Photos and player details come from
Transfermarkt, with TheSportsDB and Wikipedia as fallbacks.

The report includes two diagnostics that matter when squads look stale:
  - which season football-data.org is actually serving per league
  - players whose Transfermarkt club disagrees with the API squad
"""

import re
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


def save_db(players_dict, file_path):
    players_list = sorted(
        players_dict.values(),
        key=lambda p: (p.get('league_name', ''), p.get('team_name', ''), p.get('name', ''))
    )
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(players_list, f, indent=2, ensure_ascii=False)
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
    if not url:
        return url
    url = url.replace('/preview/', '/').replace('/small/', '/')
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
                if not tm_url:
                    print(f"    ? {player_name}: searching Transfermarkt...")
                    tm_url = search_tm_url(player_name, team_name)
                    if tm_url:
                        print(f"      -> {tm_url}")
                    time.sleep(1)

                flat = {
                    'id': player['id'],
                    'name': player_name,
                    'position': player.get('position') or old.get('position'),
                    'nationality': nationality or old.get('nationality'),
                    'team_name': team_name,
                    'league_name': league_name,
                    'team_logo_url': crest,
                    'tmUrl': tm_url,
                    'age': old.get('age'),
                    'jerseyNumber': player.get('shirtNumber') or old.get('jerseyNumber'),
                    'marketValue': old.get('marketValue'),
                    'foot': old.get('foot'),
                }

                old_url = old.get('photoUrl')
                had_photo = bool(old_url) and bool(old.get('hasPhoto'))

                if tm_url:
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
                    print(f"    ! {player_name}: no TM URL, fallbacks...")
                    flat = try_fallbacks(flat, player_name, dob, nationality, old,
                                         player_id, stats, missing_photos,
                                         league_name, team_name)

                new_db[player_id] = flat
                time.sleep(2)   # be polite to Transfermarkt

            print(f"  [OK] {len(squad)} players processed")
            time.sleep(6)

        # Players who were in this league before but are not in any current squad
        for pid in old_league_ids - new_league_ids:
            left = old_db.get(pid, {})
            transfers[league_name]['out'].append(
                f"{left.get('name', pid)} <- {left.get('team_name', '?')}")
            stats['transfers_out'] += 1

    save_db(new_db, DB_FILE)
    generate_report(stats, missing_photos, transfers, club_mismatches, seasons)

# ========================================
# REPORT
# ========================================

def generate_report(stats, missing, transfers, club_mismatches, seasons):
    total = stats['total_players']
    print("\n" + "=" * 70)
    print(f"ABSCHLUSSBERICHT  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)

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
