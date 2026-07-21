#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Football quiz database updater.

Fetches squads from football-data.org, finds player photos (TheSportsDB + Wikipedia),
detects transfers, and saves a flat JSON array for the iOS app.

Photo search priority:
  Tier 1: TheSportsDB – Sport + DOB + Nationality (highest confidence)
  Tier 2: TheSportsDB – Sport + DOB
  Tier 3: TheSportsDB – DOB only (sport tag may be missing)
  Tier 4: Wikipedia API (fallback for missing players)
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

# ========================================
# CONFIGURATION
# ========================================

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
THESPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

FOOTBALL_DATA_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

LEAGUES_TO_PROCESS = {
    "Bundesliga": 2002,
    "Serie A": 2019,
    "Ligue 1": 2015,
    "Premier League": 2021,
    "Primera Division": 2014,
}

DB_FILE = "football_quiz_complete.json"
IMAGE_DIR = "player_images"

# ========================================
# DATABASE I/O (FLAT ARRAY FORMAT)
# ========================================

def load_db(file_path):
    """Load existing flat player list. Returns dict keyed by player_id string."""
    if not os.path.exists(file_path):
        print("[INFO] No existing database found, starting fresh.")
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)

        if isinstance(data, list):
            return {str(p['id']): p for p in data if 'id' in p}

        # Legacy nested format → migrate
        if isinstance(data, dict) and 'leagues' in data:
            print("[INFO] Migrating legacy nested format to flat array...")
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
    """Save flat player array sorted by league / team / name."""
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
# IMAGE URL UTILITIES
# ========================================

def upgrade_url_resolution(url):
    """Try to get a higher-resolution version of known image URL patterns."""
    if not url:
        return url
    # TheSportsDB: remove /preview/ or /small/ subdirectory if present
    url = url.replace('/preview/', '/').replace('/small/', '/')
    # Wikipedia thumbnails: bump to 600px
    url = re.sub(r'/(\d+)px-([^/]+)$', r'/600px-\2', url)
    return url


def best_tsdb_url(player_entry):
    """Return highest-quality URL from a TheSportsDB player entry."""
    for field in ('strCutout', 'strThumb'):
        raw = player_entry.get(field)
        if raw:
            return upgrade_url_resolution(raw)
    return None

# ========================================
# PHOTO SEARCH: THESPORTSDB
# ========================================

def search_thesportsdb(player_name, dob, nationality):
    """
    3-tier DOB-based matching on TheSportsDB.
    Returns {'url': ..., 'match': ..., 'source': 'thesportsdb'} or None.
    """
    try:
        r = requests.get(
            f"{THESPORTSDB_BASE_URL}/searchplayers.php?p={quote(player_name)}",
            timeout=10
        )
        if r.status_code != 200:
            return None
        players = r.json().get('player') or []
        if not players:
            return None
    except Exception:
        return None

    nat_lower = nationality.lower() if nationality else None

    def score(p):
        """Return (tier, url) where lower tier = better confidence."""
        sport = p.get('strSport', '').lower()
        p_dob = p.get('dateBorn')
        p_nat = p.get('strNationality', '').lower()
        u = best_tsdb_url(p)

        if not u or not dob or not p_dob or p_dob != dob:
            return None  # DOB match is required for all tiers

        is_soccer = sport == 'soccer'
        nat_match = nat_lower and nat_lower in p_nat

        if is_soccer and nat_match:
            return (1, u, 'TIER1_SPORT_DOB_NAT')
        if is_soccer:
            return (2, u, 'TIER2_SPORT_DOB')
        return (3, u, 'TIER3_DOB_ONLY')

    results = [s for p in players for s in [score(p)] if s]
    if not results:
        return None

    best = min(results, key=lambda x: x[0])
    return {'url': best[1], 'match': best[2], 'source': 'thesportsdb'}

# ========================================
# PHOTO SEARCH: WIKIPEDIA (FALLBACK)
# ========================================

def search_wikipedia(player_name, nationality=None):
    """
    Searches Wikipedia for a footballer photo.
    Returns {'url': ..., 'match': 'TIER4_WIKIPEDIA', 'source': 'wikipedia'} or None.
    """
    try:
        # Step 1: article search
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

        page_title = results[0]['title']

        # Step 2: get page thumbnail
        img_r = requests.get(WIKIPEDIA_API_URL, params={
            'action': 'query', 'titles': page_title,
            'prop': 'pageimages', 'format': 'json',
            'pithumbsize': 600, 'pilicense': 'any',
        }, timeout=10)
        if img_r.status_code != 200:
            return None

        pages = img_r.json().get('query', {}).get('pages', {})
        for page in pages.values():
            source = page.get('thumbnail', {}).get('source')
            if source:
                return {
                    'url': upgrade_url_resolution(source),
                    'match': 'TIER4_WIKIPEDIA',
                    'source': 'wikipedia'
                }
    except Exception:
        pass
    return None

# ========================================
# PHOTO ORCHESTRATION
# ========================================

def find_player_photo(player_name, dob, nationality):
    """Try TheSportsDB, then Wikipedia. Returns photo dict or None."""
    result = search_thesportsdb(player_name, dob, nationality)
    if result:
        return result
    # Small pause before Wikipedia to avoid hammering APIs back-to-back
    time.sleep(0.3)
    return search_wikipedia(player_name, nationality)


def download_photo(url, save_path):
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(r.content)
            return True
        return False
    except Exception:
        return False

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

    print(f"\n[INFO] Processing {len(LEAGUES_TO_PROCESS)} leagues...")

    for league_name, league_id in LEAGUES_TO_PROCESS.items():
        print(f"\n{'='*6} {league_name} {'='*6}")

        teams = get_league_teams(league_id)

        # Detect players who left this league (were here before, not now)
        old_league_ids = {pid for pid, p in old_db.items() if p.get('league_name') == league_name}
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
                    transfers[league_name]['in'].append(f"{player_name} → {team_name}")
                    stats['transfers_in'] += 1

                # Build flat player record, preserving enriched fields (marketValue etc.)
                flat = {
                    'id': player['id'],
                    'name': player_name,
                    'position': player.get('position') or old.get('position'),
                    'nationality': nationality or old.get('nationality'),
                    'team_name': team_name,
                    'league_name': league_name,
                    'team_logo_url': crest,
                    # These fields come from external enrichment (e.g. Transfermarkt)
                    # and are preserved across runs rather than re-fetched.
                    'age': old.get('age'),
                    'jerseyNumber': player.get('shirtNumber') or old.get('jerseyNumber'),
                    'marketValue': old.get('marketValue'),
                    'tmUrl': old.get('tmUrl'),
                    'foot': old.get('foot'),
                }

                # --- PHOTO LOGIC ---
                # If a valid photo URL already exists in the DB, keep it.
                # Re-search only for new players or those with hasPhoto=False.
                existing_url = old.get('photoUrl')
                if existing_url and old.get('hasPhoto'):
                    flat['hasPhoto'] = True
                    flat['photoUrl'] = existing_url
                    flat['photoSource'] = old.get('photoSource', 'unknown')
                    flat['photo_path'] = old.get('photo_path')
                    stats['existing_photos'] += 1

                    label = "NEW (photo preserved)" if is_new else "photo OK"
                    print(f"    ✓ {player_name}: {label}")
                    new_db[player_id] = flat
                    continue

                # Need to search
                if is_new:
                    print(f"    + {player_name}: NEW – searching photo...")
                else:
                    print(f"    ✗ {player_name}: no photo – searching...")

                photo = find_player_photo(player_name, dob, nationality)

                if photo:
                    safe = re.sub(r'[^\w\-]', '_', player_name)
                    file_path = os.path.join(IMAGE_DIR, f"{safe}_{player_id}.png")

                    if download_photo(photo['url'], file_path):
                        flat['hasPhoto'] = True
                        flat['photoUrl'] = photo['url']
                        flat['photoSource'] = photo['source']
                        flat['photo_path'] = file_path
                        stats['new_photos'] += 1

                        tier = photo['match']
                        print(f"      → ✅ {tier}")

                        if tier in ('TIER1_SPORT_DOB_NAT', 'TIER2_SPORT_DOB'):
                            stats['high_confidence'] += 1
                        elif tier == 'TIER4_WIKIPEDIA':
                            stats['wikipedia'] += 1
                        else:
                            stats['low_confidence'] += 1
                    else:
                        flat['hasPhoto'] = False
                        flat['photoUrl'] = None
                        missing_photos[league_name].append(f"{player_name} ({team_name}) – download error")
                        print(f"      → ❌ download failed")
                else:
                    flat['hasPhoto'] = False
                    flat['photoUrl'] = None
                    missing_photos[league_name].append(f"{player_name} ({team_name})")
                    print(f"      → ❌ not found")

                new_db[player_id] = flat
                time.sleep(1.5)

            print(f"  [OK] {len(squad)} players processed for {team_name}")
            time.sleep(5)

        # Departures: in old league but not in any current team
        for pid in old_league_ids - new_league_ids:
            left = old_db.get(pid, {})
            transfers[league_name]['out'].append(
                f"{left.get('name', pid)} ← {left.get('team_name', '?')}"
            )
            stats['transfers_out'] += 1

    save_db(new_db, DB_FILE)
    generate_report(stats, missing_photos, transfers)

# ========================================
# REPORT
# ========================================

def generate_report(stats, missing, transfers):
    total = stats['total_players']
    new_photos = stats['new_photos']
    existing = stats['existing_photos']

    print("\n" + "=" * 70)
    print("ABSCHLUSSBERICHT")
    print("=" * 70)

    if not total:
        print("WARN: Keine Spieler verarbeitet.")
        return

    print(f"Spieler gesamt:          {total}")
    print(f"Fotos vorhanden (alt):   {existing}")
    print(f"Fotos neu gefunden:      {new_photos}")
    print(f"  Hohe Sicherheit:       {stats['high_confidence']}")
    print(f"  Wikipedia Fallback:    {stats['wikipedia']}")
    print(f"  Niedrige Sicherheit:   {stats['low_confidence']}")

    print(f"\nNeuzugänge:              {stats['transfers_in']}")
    print(f"Abgänge:                 {stats['transfers_out']}")

    for league, tx in transfers.items():
        if tx['in'] or tx['out']:
            print(f"\n  {league}:")
            if tx['in']:
                names = ', '.join(n.split('→')[0].strip() for n in tx['in'][:5])
                print(f"    + {len(tx['in'])} neu: {names}...")
            if tx['out']:
                names = ', '.join(n.split('←')[0].strip() for n in tx['out'][:5])
                print(f"    - {len(tx['out'])} weg: {names}...")

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
        if CI_RUN:
            print("[ERROR] FOOTBALL_DATA_API_KEY secret not set in GitHub repository.")
            sys.exit(1)
        else:
            print("[ERROR] FOOTBALL_DATA_API_KEY not set. Export it as environment variable:")
            print("  export FOOTBALL_DATA_API_KEY=your_key_here")
            sys.exit(1)

    print("=" * 70)
    print("FUSSBALL DATENBANK UPDATE")
    print("=" * 70)

    if CI_RUN:
        print("CI mode – running automatically...")
        compare_and_update()
    else:
        ans = input("Start full update? (ja/nein): ")
        if ans.lower() in ('ja', 'j', 'yes', 'y'):
            compare_and_update()
        else:
            print("Aborted.")
