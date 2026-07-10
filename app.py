# ─── app.py ───────────────────────────────────────────────────────────────────
"""
Arschloch – Flask-Backend.

Aufbau:
  1. Persistenz      – laufende Spiele überleben einen Server-Neustart
  2. Game-Helfer     – Sitzplätze, Host, Bots
  3. Engine          – _do_move / _settle / _tick_bots
  4. Runden & Trading
  5. Routen          – Lobby, Spiel, Rejoin, Admin, Logs
"""

import csv
import io
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from SimulatorConfig import (
    ADMIN_PASSWORD, DEFAULT_BOT_DELAY_MS, GAME_TTL_S, LOG_FILE,
    MAX_BOT_DELAY_MS, STATE_FILE, gameEntities, gameRanks, rankOrder,
)
from SimulatorFunctions import (
    allTradesDone, appendMoveLog, autoBotCitizenSwap, autoBotTrade,
    buildCitizenSwap, buildTrades, createDeck, createEntities, dealHands,
    getRank, onlyPassAvailable, pendingCitizenSwap, pendingGiveFor,
    pendingReturnFor, pendingWishFor, play, possibleMoves, randomMove,
    requiredWishCards, resolveCitizenSwap, resolveReturn, shuffleDeck,
    skipCitizenSwap, sortHand,
)

app = Flask(__name__)
CORS(app)

BOT_NAMES = ["Bot-Ada", "Bot-Turing", "Bot-Grace", "Bot-Knuth", "Bot-Lovelace"]
LIVE_STATUSES = ('lobby', 'playing', 'trading', 'finished')

games: dict = {}      # lobby_code → game
game_logs: list = []  # Rundenlogs im RAM (+ LOG_FILE als Backup)


# ══ 1. Persistenz ════════════════════════════════════════════════════════════
# Ohne das verliert man bei jedem Render-Neustart alle laufenden Runden –
# und genau das fühlt sich für Spieler wie "beim Refresh rausgeflogen" an.

_dirty = False
_last_persist = 0.0


def touch(g: Optional[dict] = None) -> None:
    """Spiel als verändert markieren (wird nach dem Request gespeichert)."""
    global _dirty
    if g is not None:
        g['touched'] = time.time()
    _dirty = True


def persist(force: bool = False) -> None:
    global _dirty, _last_persist
    if not _dirty and not force:
        return
    now = time.time()
    if not force and now - _last_persist < 1.0:
        return
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'games': games}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        _last_persist, _dirty = now, False
    except Exception as e:  # pragma: no cover
        print(f"[persist] {e}")


def load_state() -> None:
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for gid, g in (data.get('games') or {}).items():
        g.pop('pendingBotMove', None)   # Timer nach Neustart wertlos
        games[gid] = g
    for gid, g in list(games.items()):
        if g.get('status') == 'playing':
            _settle(gid, g)             # Bots ggf. wieder in Gang bringen
    print(f"[state] {len(games)} Spiele wiederhergestellt")


def cleanup_games() -> None:
    now = time.time()
    for gid, g in list(games.items()):
        if now - g.get('touched', g.get('created', now)) > GAME_TTL_S:
            games.pop(gid, None)
            touch()


@app.after_request
def _after(resp):
    persist()
    return resp


@app.errorhandler(Exception)
def _on_error(e):  # pragma: no cover
    code = getattr(e, 'code', 500)
    if code == 404:
        return jsonify({'error': 'Nicht gefunden'}), 404
    app.logger.exception(e)
    return jsonify({'error': 'Serverfehler'}), 500


# ══ 2. Game-Helfer ═══════════════════════════════════════════════════════════

def bot_slot(entity_id: int) -> dict:
    return {"id": f"bot-{entity_id}-{uuid.uuid4().hex[:4]}",
            "name": BOT_NAMES[entity_id % len(BOT_NAMES)],
            "entityID": entity_id, "isBot": True}


def get_game(gid: str) -> Optional[dict]:
    return games.get((gid or '').strip().upper())


def is_admin(value: Optional[str]) -> bool:
    return (value or '') == ADMIN_PASSWORD


def is_host(g: dict, player_id: Optional[str]) -> bool:
    return bool(player_id) and g.get('host') == player_id


def seat_of(g: dict, player_id: Optional[str]) -> Optional[int]:
    if not player_id:
        return None
    return next((p['entityID'] for p in g['players']
                 if p and p.get('id') == player_id), None)


def humans(g: dict) -> list:
    return [p for p in g['players'] if p and not p.get('isBot')]


def ensure_host(g: dict) -> None:
    """Host verlassen? Dann übernimmt der erste verbliebene Mensch."""
    if any(p.get('id') == g['host'] for p in humans(g)):
        return
    remaining = humans(g)
    g['host'] = remaining[0]['id'] if remaining else None


def bot_delay_ms(g: dict) -> int:
    return int(g.get('settings', {}).get('botDelayMs', DEFAULT_BOT_DELAY_MS))


def only_bots_left(g: dict) -> bool:
    """Solange kein Mensch mehr am Tisch sitzt, ruht das Spiel – niemand soll
    unbeobachtet weiterspielen, nur damit Bots gegeneinander Karten legen."""
    return g['status'] in ('playing', 'trading') and not humans(g)


def vacate(g: dict, slot: int, reason: str) -> None:
    """Menschen-Sitz verlassen: im Spiel übernimmt ein Bot, Platz bleibt reserviert."""
    old = g['players'][slot]
    if not old or old.get('isBot'):
        return
    if g['status'] != 'lobby':
        # Sitzplatz für Rückkehr reservieren (gleiche player_id ⇒ gleicher Platz, Host bleibt Host)
        g.setdefault('vacated', {})[old['id']] = {'slot': slot, 'name': old['name']}
        g['players'][slot] = bot_slot(slot)
        if g.get('state'):
            g['state']['players'][slot]['isBot'] = True
            g['state']['players'][slot]['name'] = g['players'][slot]['name']
            g['state']['log'].append(f'{old["name"]} {reason} – ein Bot übernimmt.')
    else:
        g['players'][slot] = None
    ensure_host(g)


# ══ 3. Engine ════════════════════════════════════════════════════════════════

def _do_move(gid: str, g: dict, eid: int, move: str, auto: bool = False) -> None:
    """Führt genau einen Zug aus (Mensch oder Bot) und rückt den Turn weiter."""
    s      = g['state']
    p      = s['players'][eid]
    is_bot = p.get('isBot', False)
    prefix = '🤖 ' if is_bot else ''

    # Kontext VOR dem Zug einfangen – landet so im CSV-Export
    ctx = dict(legal=possibleMoves(s['players'], eid, s['mostRecentMove']),
               hand=list(p['hand']),
               order=list(s['trickOrder']),
               citizen=list(s['citizenDeck']))

    # Nur ein *bewusster* Menschenzug löscht das Bot-Banner.
    if not is_bot and not auto:
        s['lastBotMove'] = None

    if move == 'pass':
        s['log'].append(f'{prefix}{p["name"]} passt{" automatisch" if auto else ""}.')
        appendMoveLog(s, eid, p['name'], 'pass', auto=auto, **ctx)
        s['passedCounter'] += 1
        if is_bot:
            _set_bot_banner(s, p['name'], 'pass', [])
    else:
        cards = play(s['players'], eid, s['stack'], move)
        p['hand'] = sortHand(p['hand'])
        s['mostRecentMove'] = move
        s['passedCounter']  = 0
        s['trickCounter']  += 1
        s['log'].append(f'{prefix}{p["name"]} spielt {move}.')
        appendMoveLog(s, eid, p['name'], move, **ctx)
        if is_bot:
            _set_bot_banner(s, p['name'], move, cards)

        if not p['hand']:
            _assign_rank(gid, g, eid, move)
            return

    _advance_turn(g, eid)


def _set_bot_banner(s: dict, name: str, move: str, cards: list) -> None:
    """Monoton steigende seq ⇒ das Frontend rendert das Banner genau einmal."""
    s['botMoveSeq'] += 1
    s['lastBotMove'] = {'seq': s['botMoveSeq'], 'name': name, 'move': move, 'cards': cards}


def _advance_turn(g: dict, eid: int) -> None:
    s = g['state']
    order = s['trickOrder']
    if not order:
        return
    if s['passedCounter'] >= len(order):
        _reset_trick(s, eid)
        return
    idx = order.index(eid) if eid in order else -1
    s['currentTurn'] = order[(idx + 1) % len(order)]


def _reset_trick(s: dict, trigger: int) -> None:
    order = s['trickOrder']
    s['log'].append('Alle gepasst – neuer Stich.')
    s['passedCounter'] = 0
    s['discardStack'].extend(s['stack'])
    s['stack'].clear()
    s['mostRecentMove'] = None
    start = order.index(trigger) if trigger in order else 0
    new_order = order[start:] + order[:start]
    s['trickOrder']     = new_order
    s['nextTrickOrder'] = new_order[:]
    s['currentTurn']    = new_order[0]


def _assign_rank(gid: str, g: dict, eid: int, move: str) -> None:
    s, remaining, order = g['state'], g['state']['remainingRanks'], g['state']['trickOrder']
    if not remaining:
        return

    # Mit einem Ass rausgehen ⇒ schlechtester noch freier Rang
    rank = remaining.pop() if getRank(move) == 'A' else remaining.pop(0)
    s['players'][eid]['rank'] = rank
    s['log'].append(f'{s["players"][eid]["name"]} fertig → {rank}!')

    idx = order.index(eid) if eid in order else 0
    if eid in order:
        order.remove(eid)
    if eid in s['nextTrickOrder']:
        s['nextTrickOrder'].remove(eid)

    if len(order) <= 1:                       # nur noch einer übrig ⇒ Runde vorbei
        if order and remaining:
            last = order[0]
            s['players'][last]['rank'] = remaining.pop(0)
            s['log'].append(f'{s["players"][last]["name"]} letzter → {s["players"][last]["rank"]}!')
        _end_round(gid, g)
        return

    s['currentTurn'] = order[idx % len(order)]


def _end_round(gid: str, g: dict) -> None:
    s = g['state']
    s['finished'] = True
    s['log'].append('🎉 Runde beendet!')
    g['status'] = 'finished'
    g.pop('pendingBotMove', None)
    g['lastRanks'] = {str(p['entityID']): p['rank'] for p in s['players']}
    save_round_log(gid, g)


def _bot_turn(gid: str, g: dict, eid: int) -> None:
    s = g['state']
    _do_move(gid, g, eid, randomMove(possibleMoves(s['players'], eid, s['mostRecentMove'])))


def _settle(gid: str, g: dict) -> None:
    """
    Bringt das Spiel in einen Zustand, in dem ein Mensch mit echten Zügen dran ist.
      • Mensch, der nur passen kann → Auto-Pass (greift auch direkt nach Bot-Zügen)
      • Bot + botDelayMs == 0       → Zug sofort
      • Bot + botDelayMs > 0        → für den nächsten Poll vormerken
    """
    for _ in range(400):
        if g['status'] != 'playing' or g['state']['finished'] or not g['state']['trickOrder']:
            g.pop('pendingBotMove', None)
            return
        if only_bots_left(g):          # (2) niemand da ⇒ Spiel ruht
            g.pop('pendingBotMove', None)
            return
        s = g['state']
        cur = s['currentTurn']
        if cur not in s['trickOrder']:
            s['currentTurn'] = s['trickOrder'][0]
            continue

        if s['players'][cur].get('isBot'):
            delay = bot_delay_ms(g)
            if delay > 0:
                g['pendingBotMove'] = {'eid': cur, 'ready_at': time.time() + delay / 1000.0}
                return
            g.pop('pendingBotMove', None)
            _bot_turn(gid, g, cur)
            continue

        if onlyPassAvailable(s['players'], cur, s['mostRecentMove']):
            _do_move(gid, g, cur, 'pass', auto=True)
            continue

        g.pop('pendingBotMove', None)
        return


def _tick_bots(gid: str, g: dict) -> None:
    """Bei jedem State-Poll: einen fälligen Bot-Zug ausführen (zeitbasiert, nicht poll-basiert)."""
    pending = g.get('pendingBotMove')
    if not pending or time.time() < pending['ready_at']:
        return
    g.pop('pendingBotMove', None)
    if only_bots_left(g):              # (2) niemand da ⇒ Spiel ruht
        return
    s, eid = g['state'], pending['eid']
    if (g['status'] == 'playing' and not s['finished']
            and s['currentTurn'] == eid and s['players'][eid].get('isBot')):
        _bot_turn(gid, g, eid)
    _settle(gid, g)
    touch(g)


# ══ 4. Runden & Trading ══════════════════════════════════════════════════════

def _init_round(gid: str, g: dict, round_number: int) -> None:
    players = createEntities([p['name'] for p in g['players']])
    for i, slot in enumerate(g['players']):
        players[i]['isBot'] = slot.get('isBot', False)

    citizenDeck: list = []
    deck = createDeck()
    shuffleDeck(deck)
    dealHands(deck, players, citizenDeck)
    for p in players:
        p['hand'] = sortHand(p['hand'])
        p['starterHand'] = list(p['hand'])

    order = list(range(gameEntities))
    random.shuffle(order)
    prev_ranks = g.get('lastRanks') or {}

    g['state'] = {
        'players':        players,
        'citizenDeck':    citizenDeck,
        'stack':          [],
        'discardStack':   [],
        'trickOrder':     order,
        'nextTrickOrder': order[:],
        'mostRecentMove': None,
        'passedCounter':  0,
        'remainingRanks': gameRanks[:gameEntities],
        'currentTurn':    order[0],
        'log':            [f'Runde {round_number} gestartet!'],
        'finished':       False,
        'roundNumber':    round_number,
        'trickCounter':   0,
        'moveLog':        [],
        'trades':         [],
        'citizenSwap':    None,
        'prevRanks':      prev_ranks,
        'lastBotMove':    None,
        'botMoveSeq':     0,
    }
    g.pop('pendingBotMove', None)

    if prev_ranks:
        for p in players:
            p['rank'] = prev_ranks.get(str(p['entityID']))
        g['state']['trades']      = buildTrades(players)
        g['state']['citizenSwap'] = buildCitizenSwap(players, citizenDeck)
        g['state']['log']         = [f'Runde {round_number} – Trading läuft!']
        for p in players:
            p['rank'] = None
        g['status'] = 'trading'
        _run_bot_trades(gid, g)
    else:
        g['status'] = 'playing'
        _settle(gid, g)


def _run_bot_trades(gid: str, g: dict) -> None:
    if only_bots_left(g):              # (2) niemand da ⇒ Spiel ruht
        return
    s = g['state']
    autoBotTrade(s['players'], s['trades'])
    for t in s['trades']:
        top, bot = s['players'][t['top_id']]['name'], s['players'][t['bot_id']]['name']
        for flag, msg in (('wish_done',   f'{top} wünscht sich Rang {t["wish_rank"]}.'),
                          ('give_done',   f'{bot} gibt Karten an {top}.'),
                          ('return_done', f'{top} gibt schlechteste Karten an {bot}.')):
            if t[flag] and not t.get('_logged_' + flag):
                s['log'].append(msg)
                t['_logged_' + flag] = True

    swap = s.get('citizenSwap')
    if autoBotCitizenSwap(s['players'], s['citizenDeck'], swap) and swap:
        name = s['players'][swap['citizen_id']]['name']
        s['log'].append(f'{name} tauscht 2 Karten mit dem Citizen-Stack.')

    if allTradesDone(s['trades'], swap):
        _finalize_trading(gid, g)


def _finalize_trading(gid: str, g: dict) -> None:
    s = g['state']
    for p in s['players']:
        p['rank'] = None
    order = list(range(gameEntities))
    random.shuffle(order)
    s.update({'trickOrder': order, 'nextTrickOrder': order[:], 'currentTurn': order[0],
              'mostRecentMove': None, 'passedCounter': 0,
              'remainingRanks': gameRanks[:gameEntities], 'lastBotMove': None})
    s['stack'].clear()
    s['discardStack'].clear()
    s['log'].append('Trading abgeschlossen – Spiel startet!')
    g['status'] = 'playing'
    g.pop('pendingBotMove', None)
    _settle(gid, g)


def _trade_partner(s: dict, trade: Optional[dict], action: Optional[str]) -> Optional[dict]:
    if not trade or action not in ('wish', 'give', 'return'):
        return None   # citizen_swap tauscht mit dem Stapel, nicht mit einem Spieler
    pid = trade['bot_id'] if action in ('wish', 'return') else trade['top_id']
    return {'name': s['players'][pid]['name'], 'entityID': pid}


# ══ Logging ══════════════════════════════════════════════════════════════════

def save_round_log(gid: str, g: dict) -> None:
    s = g.get('state')
    if not s:
        return
    record = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "lobby_code": gid,
        "game_id":    gid,
        "round":      s.get('roundNumber', 1),
        "players": [{"id": p["entityID"], "name": p["name"], "bot": p.get("isBot", False),
                     "rank": p["rank"], "starter_hand": p.get("starterHand", [])}
                    for p in s['players']],
        "moves": s.get('moveLog', []),
    }
    game_logs.append(record)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:  # pragma: no cover
        print(f"[log] {e}")


def all_log_records() -> list:
    records = list(game_logs)
    seen = {(r.get('game_id'), r.get('round'), r.get('ts')) for r in records}
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            extra = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec.get('game_id'), rec.get('round'), rec.get('ts'))
                if key not in seen:
                    extra.append(rec)
                    seen.add(key)
            records = extra + records
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return sorted(records, key=lambda r: r.get('ts', ''))


# ══ 5. Routen: Seite ═════════════════════════════════════════════════════════

@app.route('/')
def index():
    # bewusst kein render_template: Jinja2 würde das eingebettete JS zerlegen
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'games': len(games)})


# ══ Routen: Lobby & Spielübersicht ═══════════════════════════════════════════

def _game_summary(gid: str, g: dict) -> dict:
    slots  = g['players']
    empty  = sum(1 for s in slots if s is None)
    bots   = sum(1 for s in slots if s and s.get('isBot'))
    people = sum(1 for s in slots if s and not s.get('isBot'))
    host   = next((s for s in slots if s and s.get('id') == g.get('host')), None)
    return {
        'code': gid, 'status': g['status'],
        'host': host['name'] if host else '—',
        'humans': people, 'bots': bots, 'empty': empty,
        'freeSeats': empty + bots,          # Bot-Plätze sind übernehmbar
        'round': g.get('roundNumber', 0),
        'created': g.get('created', 0),
        'names': [s['name'] for s in slots if s and not s.get('isBot')],
    }


@app.route('/api/lobbies')
def list_lobbies():
    """Offene Lobbies **und** laufende Spiele – letztere kann man übernehmen/wieder betreten."""
    cleanup_games()
    out = [_game_summary(gid, g) for gid, g in games.items() if g['status'] in LIVE_STATUSES]
    out.sort(key=lambda x: x['created'], reverse=True)
    return jsonify({
        'lobbies': [x for x in out if x['status'] == 'lobby' and x['freeSeats'] > 0],
        'running': [x for x in out if x['status'] != 'lobby'],
    })


@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    name = (request.json.get('name') or 'Player').strip()[:20]
    gid  = uuid.uuid4().hex[:6].upper()
    pid  = uuid.uuid4().hex
    games[gid] = {
        'status':      'lobby',
        'host':        pid,
        'players':     [{"id": pid, "name": name, "entityID": 0, "isBot": False}] + [None] * 4,
        'state':       None,
        'roundNumber': 0,
        'lastRanks':   {},
        'vacated':     {},
        'settings':    {'botDelayMs': DEFAULT_BOT_DELAY_MS},
        'created':     time.time(),
        'touched':     time.time(),
    }
    touch(games[gid])
    return jsonify({'game_id': gid, 'player_id': pid})


def _claim_seat(g: dict, name: str, player_id: Optional[str] = None) -> Optional[tuple]:
    """
    Sucht einen Sitzplatz. Reservierter Platz (gleiche player_id) hat Vorrang,
    dann leere Plätze, dann Bot-Plätze (Übernahme).
    Gibt (slot, player_id) zurück oder None.
    """
    reserved = (g.get('vacated') or {}).get(player_id or '')
    if reserved:
        slot = reserved['slot']
        occupant = g['players'][slot]
        if occupant is None or occupant.get('isBot'):
            g['vacated'].pop(player_id, None)
            return slot, player_id            # gleiche ID ⇒ Host-Rolle bleibt erhalten

    slot = next((i for i, s in enumerate(g['players']) if s is None), None)
    if slot is None:
        slot = next((i for i, s in enumerate(g['players']) if s and s.get('isBot')), None)
    if slot is None:
        return None
    return slot, uuid.uuid4().hex


def _seat_player(gid: str, g: dict, slot: int, pid: str, name: str) -> None:
    g['players'][slot] = {"id": pid, "name": name, "entityID": slot, "isBot": False}
    if g.get('state'):
        g['state']['players'][slot]['isBot'] = False
        g['state']['players'][slot]['name']  = name
        g['state']['log'].append(f'{name} ist (wieder) dabei.')
    if g['host'] is None:
        g['host'] = pid
    pending = g.get('pendingBotMove')
    if pending and pending['eid'] == slot:
        g.pop('pendingBotMove', None)
    if g['status'] == 'playing':
        _settle(gid, g)
    elif g['status'] == 'trading':
        _run_bot_trades(gid, g)
    touch(g)


@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data = request.json or {}
    gid  = (data.get('game_id') or '').strip().upper()
    name = (data.get('name') or 'Player').strip()[:20]
    g    = get_game(gid)
    if not g:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    if g['status'] == 'aborted':
        return jsonify({'error': 'Spiel wurde abgebrochen'}), 400
    if g['status'] != 'lobby':
        return jsonify({'error': 'Spiel läuft bereits – nutze "Beitreten" bei den laufenden Spielen'}), 400

    claim = _claim_seat(g, name)
    if not claim:
        return jsonify({'error': 'Keine freien Plätze'}), 400
    slot, pid = claim
    _seat_player(gid, g, slot, pid, name)
    return jsonify({'game_id': gid, 'player_id': pid})


@app.route('/api/game/<gid>/rejoin', methods=['POST'])
def rejoin_game(gid):
    """(1) Wiedereinstieg – auch in ein laufendes Spiel. Reservierter Platz hat Vorrang."""
    data = request.json or {}
    g    = get_game(gid)
    if not g:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    if g['status'] == 'aborted':
        return jsonify({'error': 'Spiel wurde abgebrochen'}), 400

    old_pid = data.get('player_id')
    existing = seat_of(g, old_pid)
    if existing is not None:                       # sitzt bereits – idempotent
        return jsonify({'game_id': gid.upper(), 'player_id': old_pid, 'slot': existing})

    reserved = (g.get('vacated') or {}).get(old_pid or '')
    name = (data.get('name') or (reserved or {}).get('name') or 'Player').strip()[:20]

    claim = _claim_seat(g, name, old_pid)
    if not claim:
        return jsonify({'error': 'Kein freier Platz – alle Sitze sind von Menschen besetzt'}), 400
    slot, pid = claim
    _seat_player(gid.upper(), g, slot, pid, name)
    ensure_host(g)
    return jsonify({'game_id': gid.upper(), 'player_id': pid, 'slot': slot})


@app.route('/api/lobby/<gid>', methods=['GET'])
def lobby_status(gid):
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    pid = request.args.get('player_id')
    return jsonify({
        'status':   g['status'],
        'isHost':   is_host(g, pid),
        'kicked':   bool(pid) and seat_of(g, pid) is None,
        'settings': g['settings'],
        'players': [{'name': p['name'], 'entityID': p['entityID'],
                     'isBot': p.get('isBot', False), 'isHost': p.get('id') == g.get('host')}
                    if p else None for p in g['players']],
    })


@app.route('/api/lobby/<gid>/bot', methods=['POST'])
def add_bot(gid):
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not is_host(g, (request.json or {}).get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby':
        return jsonify({'error': 'Nur in der Lobby'}), 400
    slot = next((i for i, s in enumerate(g['players']) if s is None), None)
    if slot is None:
        return jsonify({'error': 'Keine freien Plätze'}), 400
    g['players'][slot] = bot_slot(slot)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/lobby/<gid>/remove_bot', methods=['POST'])
def remove_bot(gid):
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not is_host(g, data.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby':
        return jsonify({'error': 'Nur in der Lobby'}), 400
    slot = data.get('slot')
    if isinstance(slot, int) and 0 <= slot < gameEntities and g['players'][slot] \
            and g['players'][slot].get('isBot'):
        g['players'][slot] = None
        touch(g)
    return jsonify({'ok': True})


@app.route('/api/lobby/<gid>/start', methods=['POST'])
def start_game(gid):
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not is_host(g, (request.json or {}).get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby':
        return jsonify({'error': 'Läuft bereits'}), 400
    for i, slot in enumerate(g['players']):
        if slot is None:
            g['players'][i] = bot_slot(i)
    g['roundNumber'] = 1
    g['lastRanks'] = {}
    _init_round(gid.upper(), g, 1)
    touch(g)
    return jsonify({'ok': True})


# ══ Routen: Host-Rechte, Session, Verlassen ══════════════════════════════════

@app.route('/api/game/<gid>/kick', methods=['POST'])
def kick_player(gid):
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not (is_host(g, data.get('player_id')) or is_admin(data.get('password'))):
        return jsonify({'error': 'Nur Host'}), 403

    slot = data.get('slot')
    if not isinstance(slot, int) or not (0 <= slot < gameEntities) or not g['players'][slot]:
        return jsonify({'error': 'Ungültiger Platz'}), 400
    if g['players'][slot].get('id') == g.get('host') and not is_admin(data.get('password')):
        return jsonify({'error': 'Host kann sich nicht selbst kicken'}), 400

    vacate(g, slot, 'wurde gekickt')
    if g['status'] == 'playing':
        _settle(gid.upper(), g)
    elif g['status'] == 'trading':
        _run_bot_trades(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/settings', methods=['POST'])
def update_settings(gid):
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not (is_host(g, data.get('player_id')) or is_admin(data.get('password'))):
        return jsonify({'error': 'Nur Host'}), 403

    if 'botDelayMs' in data:
        try:
            delay = max(0, min(MAX_BOT_DELAY_MS, int(data['botDelayMs'])))
        except (TypeError, ValueError):
            return jsonify({'error': 'Ungültiger Wert'}), 400
        g['settings']['botDelayMs'] = delay
        if delay == 0 and g['status'] == 'playing':
            g.pop('pendingBotMove', None)
            _settle(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True, 'settings': g['settings']})


@app.route('/api/session', methods=['GET'])
def session_check():
    """
    (3) Reload-Rettung. Unterscheidet sauber zwischen
        'Sitz noch da' / 'Sitz reserviert, Rejoin möglich' / 'Spiel weg'.
    """
    gid = (request.args.get('game_id') or '').strip().upper()
    pid = request.args.get('player_id')
    g   = get_game(gid)
    if not g or g['status'] == 'aborted':
        return jsonify({'valid': False, 'exists': False, 'canRejoin': False})

    slot = seat_of(g, pid)
    if slot is not None:
        return jsonify({'valid': True, 'exists': True, 'canRejoin': False,
                        'status': g['status'], 'isHost': is_host(g, pid),
                        'name': g['players'][slot]['name'], 'game_id': gid})

    reserved  = pid in (g.get('vacated') or {})
    free_seat = any(s is None or s.get('isBot') for s in g['players'])
    return jsonify({'valid': False, 'exists': True,
                    'canRejoin': reserved or free_seat, 'reserved': reserved,
                    'status': g['status'], 'game_id': gid})


@app.route('/api/game/<gid>/leave', methods=['POST'])
def leave_game(gid):
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'ok': True})
    pid  = data.get('player_id')
    slot = seat_of(g, pid)
    if slot is None:
        return jsonify({'ok': True})

    vacate(g, slot, 'hat die Runde verlassen')
    if g['status'] == 'lobby' and not humans(g):
        games.pop(gid.upper(), None)
    elif g['status'] == 'playing':
        _settle(gid.upper(), g)
    elif g['status'] == 'trading':
        _run_bot_trades(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


# ══ Routen: Admin (Passwort) ═════════════════════════════════════════════════

@app.route('/api/admin/games', methods=['GET'])
def admin_games():
    """(2) Alle Spiele einsehen – Grundlage für Beobachten & Abbrechen."""
    if not is_admin(request.args.get('password')):
        return jsonify({'error': 'Falsches Passwort'}), 403
    cleanup_games()
    out = []
    for gid, g in games.items():
        info = _game_summary(gid, g)
        s = g.get('state')
        info['players'] = [{'name': p['name'], 'isBot': p.get('isBot', False),
                            'cards': len(p['hand']), 'rank': p['rank']}
                           for p in s['players']] if s else \
                          [{'name': p['name'], 'isBot': p.get('isBot', False),
                            'cards': 0, 'rank': None} for p in g['players'] if p]
        out.append(info)
    out.sort(key=lambda x: x['created'], reverse=True)
    return jsonify({'games': out})


@app.route('/api/game/<gid>/abort', methods=['POST'])
def abort_game(gid):
    """(2) Laufendes Spiel abbrechen – per Admin-Passwort oder als Host."""
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not (is_admin(data.get('password')) or is_host(g, data.get('player_id'))):
        return jsonify({'error': 'Falsches Passwort'}), 403

    if g.get('state') and not g['state']['finished'] and g['status'] in ('playing', 'trading'):
        save_round_log(gid.upper(), g)       # angefangene Runde trotzdem loggen
    g['status'] = 'aborted'
    g.pop('pendingBotMove', None)
    touch(g)
    return jsonify({'ok': True})


# ══ Routen: Game-State ═══════════════════════════════════════════════════════

def _spectator_view(gid: str, g: dict) -> dict:
    s = g.get('state')
    if not s:
        return {'status': g['status'], 'spectator': True, 'lobbyCode': gid,
                'players': [], 'log': [], 'myEntityID': None}
    return {
        'status': g['status'], 'spectator': True, 'lobbyCode': gid,
        'roundNumber': s['roundNumber'], 'stack': s['stack'],
        'mostRecentMove': s['mostRecentMove'], 'currentTurn': s['currentTurn'],
        'trickOrder': s['trickOrder'], 'citizenDeck': s['citizenDeck'],
        'players': [{'name': p['name'], 'entityID': p['entityID'], 'rank': p['rank'],
                     'prevRank': s['prevRanks'].get(str(p['entityID'])),
                     'cardCount': len(p['hand']), 'hand': p['hand'],
                     'isBot': p.get('isBot', False),
                     'isHost': bool(g['players'][p['entityID']])
                               and g['players'][p['entityID']].get('id') == g.get('host')}
                    for p in s['players']],
        'log': s['log'][-25:], 'finished': s['finished'],
        'lastBotMove': s.get('lastBotMove'), 'myEntityID': None,
        'myMoves': [], 'myTrade': None, 'tradeAction': None,
        'nextIsBot': 'pendingBotMove' in g, 'isHost': False,
        'settings': g['settings'], 'kicked': False,
    }


@app.route('/api/game/<gid>/state', methods=['GET'])
def get_state(gid):
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    gid = gid.upper()

    if g['status'] == 'aborted':
        return jsonify({'status': 'aborted', 'lobbyCode': gid})

    password  = request.args.get('password')
    player_id = request.args.get('player_id')

    if g['status'] != 'lobby':
        _tick_bots(gid, g)

    if is_admin(password) and seat_of(g, player_id) is None:
        return jsonify(_spectator_view(gid, g))

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby', 'lobbyCode': gid})

    seat = seat_of(g, player_id)
    if seat is None:
        free = any(s is None or s.get('isBot') for s in g['players'])
        return jsonify({'status': g['status'], 'lobbyCode': gid, 'kicked': True,
                        'canRejoin': free or player_id in (g.get('vacated') or {}),
                        'myEntityID': None})

    s = g['state']
    my_moves = (possibleMoves(s['players'], seat, s['mostRecentMove'])
                if g['status'] == 'playing' and seat == s['currentTurn'] and not s['finished']
                else [])

    my_trade = trade_action = None
    if g['status'] == 'trading':
        for action, finder in (('wish', pendingWishFor), ('give', pendingGiveFor),
                               ('return', pendingReturnFor)):
            found = finder(s['trades'], seat)
            if found:
                my_trade, trade_action = found, action
                break
        if my_trade is None:
            citizen = pendingCitizenSwap(s.get('citizenSwap'), seat)
            if citizen:
                my_trade, trade_action = citizen, 'citizen_swap'

    return jsonify({
        'status':         g['status'],
        'lobbyCode':      gid,
        'roundNumber':    s['roundNumber'],
        'players': [{'name': p['name'], 'entityID': p['entityID'], 'rank': p['rank'],
                     'prevRank': s['prevRanks'].get(str(p['entityID'])),
                     'cardCount': len(p['hand']),
                     'hand': p['hand'] if p['entityID'] == seat else [],
                     'isBot': p.get('isBot', False),
                     'isHost': bool(g['players'][p['entityID']])
                               and g['players'][p['entityID']].get('id') == g.get('host')}
                    for p in s['players']],
        'stack':          s['stack'],
        'mostRecentMove': s['mostRecentMove'],
        'currentTurn':    s['currentTurn'],
        'myEntityID':     seat,
        'myMoves':        my_moves,
        'myTrade':        my_trade,
        'tradeAction':    trade_action,
        'tradePartner':   _trade_partner(s, my_trade, trade_action),
        'log':            s['log'][-18:],
        'finished':       s['finished'],
        'lastBotMove':    s.get('lastBotMove'),
        'nextIsBot':      'pendingBotMove' in g,
        'isHost':         is_host(g, player_id),
        'settings':       g['settings'],
        'spectator':      False,
        'kicked':         False,
    })


@app.route('/api/game/<gid>/move', methods=['POST'])
def make_move(gid):
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if g['status'] != 'playing':
        return jsonify({'error': 'Nicht in Spielphase'}), 400

    s    = g['state']
    seat = seat_of(g, data.get('player_id'))
    if seat is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403
    if seat != s['currentTurn']:
        return jsonify({'error': 'Nicht dein Zug'}), 400

    move = data.get('move')
    if move not in possibleMoves(s['players'], seat, s['mostRecentMove']):
        return jsonify({'error': 'Ungültiger Zug'}), 400

    _do_move(gid.upper(), g, seat, move)
    _settle(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


# ══ Routen: Trading ══════════════════════════════════════════════════════════

def _trade_ctx(gid, finder, action_name):
    """Gemeinsame Validierung der drei Trading-Schritte."""
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return None, jsonify({'error': 'Nicht gefunden'}), 404
    if g['status'] != 'trading':
        return None, jsonify({'error': 'Nicht in Trading-Phase'}), 400
    seat = seat_of(g, data.get('player_id'))
    if seat is None:
        return None, jsonify({'error': 'Unbekannter Spieler'}), 403
    trade = finder(g['state']['trades'], seat)
    if not trade:
        return None, jsonify({'error': f'Kein {action_name} für dich'}), 400
    return (g, seat, trade, data), None, None


def _check_cards(hand: list, cards: list, count: int):
    if len(cards) != count:
        return jsonify({'error': f'Bitte genau {count} Karte(n) wählen'}), 400
    pool = list(hand)
    for c in cards:
        if c not in pool:
            return jsonify({'error': f'Karte {c} nicht in deiner Hand'}), 400
        pool.remove(c)
    return None


@app.route('/api/game/<gid>/trade/wish', methods=['POST'])
def submit_wish(gid):
    ctx, err, code = _trade_ctx(gid, pendingWishFor, 'Wunsch')
    if err:
        return err, code
    g, seat, trade, data = ctx
    wish = (data.get('wish_rank') or '').strip()
    if wish not in rankOrder:
        return jsonify({'error': 'Ungültiger Rang'}), 400

    trade.update(wish_rank=wish, wish_done=True, _logged_wish_done=True)
    g['state']['log'].append(f'{g["state"]["players"][seat]["name"]} wünscht sich Rang {wish}.')
    _run_bot_trades(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/trade/give', methods=['POST'])
def submit_give(gid):
    ctx, err, code = _trade_ctx(gid, pendingGiveFor, 'Give')
    if err:
        return err, code
    g, seat, trade, data = ctx
    s, cards = g['state'], data.get('cards') or []
    hand = s['players'][seat]['hand']

    bad = _check_cards(hand, cards, trade['count'])
    if bad:
        return bad

    must = requiredWishCards(hand, trade['wish_rank'], trade['count'])
    if sum(1 for c in cards if getRank(c) == trade['wish_rank']) < must:
        return jsonify({'error': f'Du musst {must} Karte(n) vom Rang {trade["wish_rank"]} abgeben!'}), 400

    for c in cards:
        hand.remove(c)
        s['players'][trade['top_id']]['hand'].append(c)
    for p in s['players']:
        p['hand'] = sortHand(p['hand'])

    trade.update(give_done=True, _logged_give_done=True)
    s['log'].append(f'{s["players"][seat]["name"]} gibt {len(cards)} Karte(n) ab.')
    _run_bot_trades(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/trade/return', methods=['POST'])
def submit_return(gid):
    ctx, err, code = _trade_ctx(gid, pendingReturnFor, 'Return')
    if err:
        return err, code
    g, seat, trade, data = ctx
    s, cards = g['state'], data.get('cards') or []

    bad = _check_cards(s['players'][seat]['hand'], cards, trade['count'])
    if bad:
        return bad

    resolveReturn(s['players'], trade, cards)
    trade.update(return_done=True, _logged_return_done=True)
    s['log'].append(f'{s["players"][seat]["name"]} gibt {len(cards)} Karte(n) '
                    f'an {s["players"][trade["bot_id"]]["name"]} zurück.')
    if allTradesDone(s['trades'], s.get('citizenSwap')):
        _finalize_trading(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/trade/citizen_swap', methods=['POST'])
def submit_citizen_swap(gid):
    """Citizen tauscht 2 Handkarten gegen 2 zufällige Karten aus dem Citizen-Stack (optional)."""
    data = request.json or {}
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if g['status'] != 'trading':
        return jsonify({'error': 'Nicht in Trading-Phase'}), 400
    seat = seat_of(g, data.get('player_id'))
    if seat is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403

    s = g['state']
    swap = pendingCitizenSwap(s.get('citizenSwap'), seat)
    if not swap:
        return jsonify({'error': 'Kein Tausch für dich verfügbar'}), 400

    if data.get('skip'):
        skipCitizenSwap(swap)
        s['log'].append(f'{s["players"][seat]["name"]} verzichtet auf den Citizen-Tausch.')
    else:
        cards = data.get('cards') or []
        bad = _check_cards(s['players'][seat]['hand'], cards, swap['count'])
        if bad:
            return bad
        resolveCitizenSwap(s['players'], s['citizenDeck'], swap, cards)
        s['log'].append(f'{s["players"][seat]["name"]} tauscht {len(cards)} Karte(n) mit dem Citizen-Stack.')

    if allTradesDone(s['trades'], swap):
        _finalize_trading(gid.upper(), g)
    touch(g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/next_round', methods=['POST'])
def next_round(gid):
    g = get_game(gid)
    if not g:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if not is_host(g, (request.json or {}).get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'finished':
        return jsonify({'error': 'Runde läuft noch'}), 400
    g['roundNumber'] += 1
    _init_round(gid.upper(), g, g['roundNumber'])
    touch(g)
    return jsonify({'ok': True})


# ══ Routen: Logs (JSON + CSV) ════════════════════════════════════════════════

CSV_COLUMNS = ['ts', 'lobby_code', 'round', 'trick', 'move_index',
               'player_id', 'player_name', 'is_bot', 'move', 'auto',
               'legal_moves', 'active_hand', 'trick_order', 'citizen_stack',
               'final_rank', 'starter_hand']


@app.route('/api/logs', methods=['GET'])
def get_logs():
    if not is_admin(request.args.get('password')):
        return jsonify({'error': 'Falsches Passwort'}), 403
    return jsonify({'logs': all_log_records()})


@app.route('/api/logs.csv', methods=['GET'])
def get_logs_csv():
    if not is_admin(request.args.get('password')):
        return jsonify({'error': 'Falsches Passwort'}), 403

    only = (request.args.get('lobby') or '').strip().upper()
    buf  = io.StringIO()
    w    = csv.writer(buf, delimiter=';')
    w.writerow(CSV_COLUMNS)

    def cell(seq):
        return ' '.join(str(x) for x in (seq or []))

    for rec in all_log_records():
        code = rec.get('lobby_code') or rec.get('game_id', '')
        if only and code != only:
            continue
        by_id = {p['id']: p for p in rec.get('players', [])}
        for i, m in enumerate(rec.get('moves', [])):
            p = by_id.get(m.get('p'), {})
            w.writerow([
                rec.get('ts', ''), code, m.get('round', ''), m.get('trick', ''), i,
                m.get('p', ''), m.get('name', ''),
                'ja' if p.get('bot') else 'nein',
                m.get('move', ''), 'ja' if m.get('auto') else 'nein',
                cell(m.get('legal')),      # (4) legal moves
                cell(m.get('hand')),       # (4) active hand (vor dem Zug)
                cell(m.get('order')),      # (4) trick order
                cell(m.get('citizen')),    # (4) citizen stack
                p.get('rank') or '',
                cell(p.get('starter_hand')),
            ])

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
    name  = f'arschloch-logs-{only or "alle"}-{stamp}.csv'
    # BOM ⇒ Excel liest Umlaute und Kartensymbole korrekt
    return Response('\ufeff' + buf.getvalue(),
                    mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


# ══ Run ══════════════════════════════════════════════════════════════════════

load_state()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
