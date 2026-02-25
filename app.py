from datetime import datetime
from calendar import monthrange
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config["SECRET_KEY"] = "deathroll-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

from games.duel import init_duel
init_duel(app, socketio)

from services.imgconvert import bp as imgconvert_bp
app.register_blueprint(imgconvert_bp)
# ---------------- Deathroll PvP ----------------
pvp_queue = []
pvp_rooms = {}

sid_to_room = {}

# ---------------- Blackjack PvP ----------------
bj_queue = []
bj_rooms = {}
bj_sid_to_room = {}

# ---------------- Time calculator ----------------

SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
    "decade": 10 * 365 * 86400,
}

TIME_ORDER = ["decade", "year", "month", "day", "hour", "minute", "second"]


def time_convert(value, unit):
    total_seconds = value * SECONDS[unit]
    return {u: total_seconds / SECONDS[u] for u in TIME_ORDER}

# ---------------- Month calculator ----------------

def add_months(start, months):
    total_months = (start.year * 12) + (start.month - 1) + months
    year, month_index = divmod(total_months, 12)
    month = month_index + 1
    day = min(start.day, monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def calendar_diff(start, end):
    if end < start:
        start, end = end, start

    total_months = (end.year - start.year) * 12 + (end.month - start.month)
    anchor = add_months(start, total_months)
    if anchor > end:
        total_months -= 1
        anchor = add_months(start, total_months)

    years, months = divmod(total_months, 12)
    remainder = end - anchor
    days = remainder.days
    hours, remainder_seconds = divmod(remainder.seconds, 3600)
    minutes, seconds = divmod(remainder_seconds, 60)

    return {
        "Year": years,
        "Month": months,
        "Day": days,
        "Hour": hours,
        "Min": minutes,
        "Sec": seconds,
    }


def elapsed_time_convert(start, end):
    total_seconds = abs((end - start).total_seconds())
    month_order = ["year", "month", "day", "hour", "minute", "second"]
    return {u.capitalize(): total_seconds / SECONDS[u] for u in month_order}



# ---------------- Resolution calculator ----------------

def resolution_convert(w, h, scales):
    out = []
    for s in scales:
        out.append({
            "scale": s,
            "w": round(w * s),
            "h": round(h * s),
        })
    return out

# ---------------- Drive price calculator ----------------

def drive_price_calc(drives):
    """
    drives: list of (tb, price)
    returns: (results, cheapest)
    """
    results = []
    for tb, price in drives:
        dptb = price / tb
        results.append((tb, price, dptb))

    cheapest = min(results, key=lambda x: x[2])
    return results, cheapest

# ---------------- Hard drive usable space calculator ----------------

DECIMAL_UNITS = {
    "GB": 10**9,
    "TB": 10**12,
}

def usable_space_calc(capacity_value, capacity_unit, overhead_percent, reserved_gb):
    total_bytes = capacity_value * DECIMAL_UNITS[capacity_unit]
    formatted_bytes = total_bytes * (1 - overhead_percent / 100)
    reserved_bytes = reserved_gb * DECIMAL_UNITS["GB"]
    usable_bytes = max(formatted_bytes - reserved_bytes, 0)

    return {
        "total_bytes": total_bytes,
        "formatted_bytes": formatted_bytes,
        "reserved_bytes": reserved_bytes,
        "usable_bytes": usable_bytes,
        "usable_decimal_gb": usable_bytes / DECIMAL_UNITS["GB"],
        "usable_decimal_tb": usable_bytes / DECIMAL_UNITS["TB"],
        "usable_binary_gib": usable_bytes / (2**30),
        "usable_binary_tib": usable_bytes / (2**40),
        "binary_capacity_gib": total_bytes / (2**30),
        "binary_capacity_tib": total_bytes / (2**40),
    }

# ---------------- Power bill calculator ----------------

POWER_PROVIDERS = [
    {
        "id": "bc_hydro",
        "name": "BC Hydro (British Columbia)",
        "rate": 0.1097,
        "currency": "CAD",
        "updated": "2024-04",
    },
    {
        "id": "hydro_quebec",
        "name": "Hydro-Québec (Québec)",
        "rate": 0.0730,
        "currency": "CAD",
        "updated": "2024-04",
    },
    {
        "id": "hydro_one",
        "name": "Hydro One (Ontario)",
        "rate": 0.103,
        "currency": "CAD",
        "updated": "2024-04",
    },
    {
        "id": "pge",
        "name": "PG&E (Northern California)",
        "rate": 0.41,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "sce",
        "name": "Southern California Edison (California)",
        "rate": 0.35,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "sdge",
        "name": "SDG&E (San Diego, California)",
        "rate": 0.46,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "coned",
        "name": "Con Edison (New York)",
        "rate": 0.27,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "duke",
        "name": "Duke Energy (Carolinas)",
        "rate": 0.13,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "fpl",
        "name": "Florida Power & Light (Florida)",
        "rate": 0.14,
        "currency": "USD",
        "updated": "2024-04",
    },
    {
        "id": "oncor",
        "name": "Oncor (Texas)",
        "rate": 0.14,
        "currency": "USD",
        "updated": "2024-04",
    },
]

POWER_PROVIDER_LOOKUP = {provider["id"]: provider for provider in POWER_PROVIDERS}


def power_bill_calc(wattage, provider_id):
    provider = POWER_PROVIDER_LOOKUP[provider_id]
    kwh_year = (wattage * 24 * 365) / 1000
    yearly_cost = kwh_year * provider["rate"]
    monthly_cost = yearly_cost / 12
    return {
        "provider": provider,
        "kwh_year": kwh_year,
        "yearly_cost": yearly_cost,
        "monthly_cost": monthly_cost,
    }

# ---------------- Darkmoon flavor text ----------------

# thresholds
CRIT_SUCCESS_THRESHOLD = 95
CRIT_FAILURE_THRESHOLD = 5

FLAVOR_TEXT = {
    "hostile": [
        "The cards turn against you. Fate is not merely unkind — it is hostile.",
        "Dark energies coil around the spread. The Faire offers no mercy.",
        "The deck recoils. Whatever you attempt, expect resistance.",
        "You were not simply unlucky. You were actively opposed.",
    ],
    "poor": [
        "The cards waver uneasily. Fortune does not favor you today.",
        "The spread is weak, uncertain, and unreliable.",
        "Luck is thin here. Proceed, but expect setbacks.",
        "The Darkmoon cards whisper doubt and hesitation.",
    ],
    "favorable": [
        "The cards align, though imperfectly. Fortune leans your way.",
        "A modest but usable fate reveals itself.",
        "The spread shows promise, if not certainty.",
        "Luck is present, but it demands effort.",
    ],
    "strong": [
        "The cards glow faintly. Fortune is firmly on your side.",
        "A strong alignment forms across the spread.",
        "The cards smile upon this outcome.",
        "Luck gathers, steady and reliable.",
    ],
    "overwhelming": [
        "The cards blaze with power. Fate bends willingly.",
        "This is no coincidence. Fortune has chosen you.",
        "Overwhelming fortune surges through the spread.",
        "The deck sings. Victory is inevitable.",
    ],
}

CRITICAL_TEXT = {
    "success": [
        "A perfect draw! The deck smiles upon you in full glory.",
        "Fate itself bends to your will.",
        "The cards blaze with overwhelming power — victory is assured!",
    ],
    "failure": [
        "A catastrophic spread! The cards conspire against you.",
        "Critical failure! Nothing goes your way.",
        "The deck frowns. Misfortune overwhelms all attempts.",
    ],
}

DECK_FLAVOR = {
    "Furies": [
        "Relentless wrath courses through the spread.",
        "The cards burn with barely restrained fury.",
        "Anger and retribution press heavily upon fate.",
    ],

    "Nightmares": [
        "Distorted visions coil through the cards.",
        "The spread reeks of dread and broken dreams.",
        "Unsettling omens seep from every draw.",
    ],

    "Deception": [
        "Illusions twist the truth beyond recognition.",
        "The cards conceal as much as they reveal.",
        "Nothing in this spread is as it appears.",
    ],

    "Vengeance": [
        "Old debts demand to be answered.",
        "The deck remembers every slight.",
        "Retribution waits patiently within the cards.",
    ],

    "Commendation": [
        "Recognition glimmers faintly within the spread.",
        "The cards acknowledge effort, if not triumph.",
        "Merit is noted, though rewards remain uncertain.",
    ],

    "Resurrection": [
        "Faded fortunes stir back toward life.",
        "What was lost may yet return altered.",
        "The deck hums with renewed possibility.",
    ],

    "War": [
        "The spread echoes with the din of battle.",
        "Victory and loss hang in fragile balance.",
        "The deck rumbles...",
    ],

    "Tragedy": [
        "Sorrow weighs heavily upon the cards.",
        "The spread speaks of loss long endured.",
        "Fate turns cruel and unyielding.",
    ],

    "Madness": [
        "Reason fractures beneath chaotic forces.",
        "The cards refuse orderly interpretation.",
        "Unstable energies warp the spread.",
    ],

    "Hopes": [
        "A fragile optimism lingers within the cards.",
        "Possibility flickers, uncertain but present.",
        "The spread suggests promise not yet realized.",
    ],

    "Fables": [
        "Ancient stories whisper through the draw.",
        "Lessons of old shape the present fate.",
        "Myth and meaning entwine within the cards.",
    ],

    "Dominion": [
        "Authority asserts itself across the spread.",
        "Power gathers, demanding command.",
        "The cards favor control and resolve.",
    ],

    "Judgment": [
        "Actions are weighed with impartial clarity.",
        "The cards offer no mercy, only truth.",
        "Consequences reveal themselves without bias.",
    ],
}

def darkmoon_flavor_from_chance(chance, deck):
    # ---------- Critical results override EVERYTHING ----------
    if chance >= CRIT_SUCCESS_THRESHOLD:
        return random.choice(CRITICAL_TEXT["success"])

    if chance <= CRIT_FAILURE_THRESHOLD:
        return random.choice(CRITICAL_TEXT["failure"])

    # ---------- Normal tier flavor ----------
    if chance < 25:
        tier = "hostile"
    elif chance < 50:
        tier = "poor"
    elif chance < 75:
        tier = "favorable"
    elif chance < 95:
        tier = "strong"
    else:
        tier = "overwhelming"

    base = random.choice(FLAVOR_TEXT[tier])

    # ---------- Deck overlay (non-critical only) ----------
    if deck in DECK_FLAVOR:
        overlay = random.choice(DECK_FLAVOR[deck])
        return f"{base} {overlay}"

    return base

# ---------------- Darkmoon luck calculator ----------------

import random

CARD_VALUES = {
    "Ace": 10,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "8": 8, "9": 9, "10": 10,
    "Jack": -5,
    "Queen": -8,
    "King": -10,
}

DIFFICULTY = {
    "trivial": 20,
    "normal": 40,
    "epic": 70,
    "legendary": 100,
}

def darkmoon_draw_cards(n):
    """
    n: number of cards to draw
    returns: list of (card_name, value)
    """
    return random.choices(list(CARD_VALUES.items()), k=n)


def darkmoon_apply_deck(draws, deck):
    """
    draws: list of (card, value)
    deck: deck name (string)
    returns: modified luck score (float)
    """
    values = [v for _, v in draws]

    if deck == "Judgment":
        return sum(values)

    if deck == "Commendation":
        return sum(values) * 1.1

    if deck == "Hopes":
        return sum(values) + 5

    if deck == "Furies":
        return sum(v * 1.3 if v > 0 else v * 0.8 for v in values)

    if deck == "Vengeance":
        return sum(v * 1.4 if v > 0 else v * 1.2 for v in values)

    if deck == "War":
        return sum(v * random.uniform(0.5, 1.8) for v in values)

    if deck == "Nightmares":
        return sum(v * random.uniform(0.5, 1.1) for v in values)

    if deck == "Tragedy":
        return sum(v * 0.7 if v > 0 else v * 1.5 for v in values)

    if deck == "Resurrection":
        return sum(v if v > 0 else v * 0.3 for v in values)

    if deck == "Deception":
        avg = sum(values) / len(values)
        return avg * len(values)

    if deck == "Madness":
        return sum(v * random.uniform(0.3, 2.0) for v in values)

    if deck == "Fables":
        return sum(v * random.uniform(0.9, 1.3) for v in values)

    if deck == "Dominion":
        total = sum(values)
        return total * 1.5 if total > 0 else total * 1.3

    raise ValueError("Unknown deck")


def darkmoon_luck_calc(num_cards, deck, difficulty):
    """
    num_cards: int
    deck: string
    difficulty: string
    returns: dict with score, chance, cards
    """
    draws = darkmoon_draw_cards(num_cards)
    score = darkmoon_apply_deck(draws, deck)

    required = DIFFICULTY[difficulty]
    chance = max(0, min(100, int((score / required) * 100)))

    return {
        "score": int(score),
        "chance": chance,
        "cards": [card for card, _ in draws],
        "deck": deck,
        "difficulty": difficulty.capitalize(),
        "comment": darkmoon_flavor_from_chance(chance, deck),
    }


# ---------------- Routes ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/time", methods=["GET", "POST"])
def time_calc():
    results = None
    if request.method == "POST":
        value = float(request.form["value"])
        unit = request.form["unit"]
        results = time_convert(value, unit)
    return render_template("time.html", results=results)


@app.route("/month", methods=["GET", "POST"])
def month_calc():
    results = None
    range_text = None
    if request.method == "POST":
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        start_time = request.form.get("start_time") or "00:00:00"
        end_time = request.form.get("end_time") or "00:00:00"
        show_start_time = bool(request.form.get("start_time"))
        show_end_time = bool(request.form.get("end_time"))
        if len(start_time) == 5:
            start_time = f"{start_time}:00"
        if len(end_time) == 5:
            end_time = f"{end_time}:00"
        start = datetime.fromisoformat(f"{start_date}T{start_time}")
        end = datetime.fromisoformat(f"{end_date}T{end_time}")
        results = elapsed_time_convert(start, end)
        start_format = "%b %d, %Y %H:%M:%S" if show_start_time else "%b %d, %Y"
        end_format = "%b %d, %Y %H:%M:%S" if show_end_time else "%b %d, %Y"
        range_text = f"{start.strftime(start_format)} - {end.strftime(end_format)}"
    return render_template("month.html", results=results, range_text=range_text)


@app.route("/resolution", methods=["GET", "POST"])
def resolution_calc():
    results = None
    if request.method == "POST":
        w = int(request.form["width"])
        h = int(request.form["height"])
        scales = [float(x) for x in request.form["scales"].split(",")]
        results = resolution_convert(w, h, scales)
    return render_template("resolution.html", results=results)


@app.route("/drives", methods=["GET", "POST"])
def drives_calc():
    results = None
    cheapest = None
    error = None

    if request.method == "POST":
        raw = request.form["drives"].strip().splitlines()
        drives = []

        try:
            for line in raw:
                tb, price = line.split(":")
                tb = float(tb)
                price = float(price)
                if tb <= 0 or price < 0:
                    raise ValueError
                drives.append((tb, price))

            results, cheapest = drive_price_calc(drives)

        except Exception:
            error = "Invalid format. Use one drive per line: TB:PRICE (e.g. 8:160)"

    return render_template(
        "drives.html",
        results=results,
        cheapest=cheapest,
        error=error
    )

@app.route("/usable-space", methods=["GET", "POST"])
def usable_space():
    result = None
    error = None

    if request.method == "POST":
        try:
            capacity_value = float(request.form["capacity_value"])
            capacity_unit = request.form["capacity_unit"]
            overhead_percent = float(request.form["overhead_percent"])
            reserved_gb = float(request.form["reserved_gb"])

            if capacity_value <= 0 or overhead_percent < 0 or reserved_gb < 0:
                raise ValueError

            result = usable_space_calc(
                capacity_value,
                capacity_unit,
                overhead_percent,
                reserved_gb,
            )
        except Exception:
            error = "Enter valid positive numbers for capacity, overhead, and reserved space."

    return render_template("usable_space.html", result=result, error=error)

@app.route("/power-bill", methods=["GET", "POST"])
def power_bill():
    result = None
    error = None
    if request.method == "POST":
        try:
            wattage = float(request.form["wattage"])
            provider_id = request.form["provider"]
            if wattage <= 0:
                raise ValueError
            if provider_id not in POWER_PROVIDER_LOOKUP:
                raise ValueError
            result = power_bill_calc(wattage, provider_id)
        except Exception:
            error = "Enter a valid wattage and select a power provider."
    return render_template(
        "power_bill.html",
        result=result,
        error=error,
        providers=POWER_PROVIDERS,
    )


@app.route("/darkmoon", methods=["GET", "POST"])
def darkmoon():
    result = None
    if request.method == "POST":
        result = darkmoon_luck_calc(
            int(request.form["cards"]),
            request.form["deck"],
            request.form["difficulty"],
        )
    return render_template("darkmoon.html", result=result)

@app.route("/deathroll")
def deathroll():
    return render_template("deathroll.html")

@app.route("/deathroll-pvp")
def deathroll_pvp():
    return render_template("deathroll_pvp.html")

@app.route("/blackjack")
def blackjack():
    return render_template("blackjack.html")

@app.route("/blackjack-pvp")
def blackjack_pvp():
    return render_template("blackjack_pvp.html")

@socketio.on("queue")
def handle_queue():
    sid = request.sid

    # prevent double-queue
    if sid in pvp_queue:
        emit("system", "Already queued.")
        return

    # prevent re-queue while in match
    if sid in sid_to_room:
        emit("system", "You are already in a match.")
        return

    pvp_queue.append(sid)
    emit("system", "Queued. Waiting for opponent...")

    if len(pvp_queue) >= 2:
        p1 = pvp_queue.pop(0)
        p2 = pvp_queue.pop(0)

        room = f"room-{p1[:5]}-{p2[:5]}"
        pvp_rooms[room] = {
            "players": [p1, p2],
            "bet": {},
            "max": 1000,
            "turn": p1,
            "finished": False,
        }

        sid_to_room[p1] = room
        sid_to_room[p2] = room

        join_room(room, sid=p1)
        join_room(room, sid=p2)

        # tell each client who they are
        socketio.emit("role", "PlayerA", to=p1)
        socketio.emit("role", "PlayerB", to=p2)

        emit("system", "Match found! Agree on a bet.", to=room)


@socketio.on("bet")
def handle_bet(amount):
    sid = request.sid

    for room, game in pvp_rooms.items():
        if sid in game["players"]:
            game["bet"][sid] = amount

            emit("system", f"Bet set: {amount}g", to=room)

            if len(set(game["bet"].values())) == 1 and len(game["bet"]) == 2:
                emit("system", "Bets locked. Type /roll 1000 to start.", to=room)
            return

@socketio.on("roll")
def handle_roll(max_roll):
    sid = request.sid

    for room, game in pvp_rooms.items():
        if sid != game["turn"]:
            continue
        if game.get("finished"):
            emit("system", "The match is over. You can keep chatting here.", to=sid)
            return

        bet_values = list(game.get("bet", {}).values())
        if len(bet_values) < 2 or len(set(bet_values)) != 1:
            emit("system", "Both players must set the same bet before rolling.", to=sid)
            return

        if int(max_roll) != int(game["max"]):
            emit("system", f"Invalid roll. You must /roll {game['max']}.", to=sid)
            return

        roll = random.randint(1, int(max_roll))
        players = game["players"]
        label = "PlayerA" if sid == players[0] else "PlayerB"
        emit("chat", f"{label} rolled {roll} (1–{max_roll})", to=room)

        if roll == 1:
            loser_role = label
            winner_role = "PlayerB" if label == "PlayerA" else "PlayerA"

            bet_values = list(game.get("bet", {}).values())
            bet = bet_values[0] if len(bet_values) == 2 and len(set(bet_values)) == 1 else 0

            emit("system", f"{label} loses the deathroll.", to=room)
            socketio.emit("result", {"winner": winner_role, "loser": loser_role, "bet": bet}, to=room)
            game["finished"] = True
            return

        game["max"] = roll
        game["turn"] = next(p for p in players if p != sid)
        return

    # If we didn't find a match where it's your turn:
    emit("system", "Not your turn (or you're not in a match).", to=sid)


@socketio.on("chat")
def on_chat(msg):
    sid = request.sid
    room = sid_to_room.get(sid)

    if not room or room not in pvp_rooms:
        emit("system", "You are not in a match.")
        return

    if not isinstance(msg, str) or not msg.strip():
        return

    players = pvp_rooms[room]["players"]
    label = "PlayerA" if sid == players[0] else "PlayerB"

    socketio.emit("chat", f"{label}: {msg.strip()}", to=room)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    
    # Clean up deathroll queue and rooms
    if sid in pvp_queue:
        pvp_queue.remove(sid)

    room = sid_to_room.pop(sid, None)
    if room and room in pvp_rooms:
        game = pvp_rooms[room]
        players = game.get("players", [])
        label = "PlayerA" if players and sid == players[0] else "PlayerB"

        leave_room(room, sid=sid)
        emit("system", f"{label} leaves the instance.", to=room)

        if sid in players:
            players.remove(sid)
        if not players:
            pvp_rooms.pop(room, None)
    
    # Clean up blackjack queue and rooms
    if sid in bj_queue:
        bj_queue.remove(sid)
    
    bj_room = bj_sid_to_room.pop(sid, None)
    if bj_room and bj_room in bj_rooms:
        game = bj_rooms[bj_room]
        players = game.get("players", [])
        
        p1, p2 = players if len(players) == 2 else (None, None)
        label = "P1" if p1 and sid == p1 else "P2"
        
        leave_room(bj_room, sid=sid)
        emit("bj_system", f"{label} disconnected.", to=bj_room)
        
        # If the other player is still there, clean up their mapping too
        if sid in players:
            players.remove(sid)
        if not players:
            bj_rooms.pop(bj_room, None)


@socketio.on("bj_queue")
def bj_queue_up():
    sid = request.sid

    if sid in bj_queue:
        emit("bj_system", "Already queued.")
        return

    existing = bj_sid_to_room.get(sid)
    if existing and existing in bj_rooms:
        game = bj_rooms[existing]
        if not game.get("finished"):
            emit("bj_system", "You are already in an active Blackjack match.")
            return
        else:
            # Match is finished, clean up the old room mapping
            bj_sid_to_room.pop(sid, None)
            # Clean up the room if both players have left
            players = game.get("players", [])
            if all(p not in bj_sid_to_room for p in players):
                bj_rooms.pop(existing, None)

    bj_queue.append(sid)
    emit("bj_system", "Queued for Blackjack PvP. Waiting for opponent...")

    if len(bj_queue) >= 2:
        p1 = bj_queue.pop(0)
        p2 = bj_queue.pop(0)

        room = f"bj-{p1[:5]}-{p2[:5]}"
        bj_rooms[room] = {
            "players": [p1, p2],
            "bet": {},
            "deck": [],
            "hands": {p1: [], p2: []},
            "done": {p1: False, p2: False},
            "active": p1,
            "in_round": False,
            "finished": False,
        }

        bj_sid_to_room[p1] = room
        bj_sid_to_room[p2] = room

        join_room(room, sid=p1)
        join_room(room, sid=p2)

        socketio.emit("bj_role", "P1", to=p1)
        socketio.emit("bj_role", "P2", to=p2)
        emit("bj_system", "Match found! Both players set the same bet, then Deal.", to=room)


@socketio.on("bj_bet")
def bj_set_bet(amount):
    sid = request.sid
    room = bj_sid_to_room.get(sid)
    if not room or room not in bj_rooms:
        emit("bj_system", "You are not in a Blackjack match.")
        return

    game = bj_rooms[room]
    if game.get("finished"):
        emit("bj_system", "Match is over. Queue again to play.", to=sid)
        return

    try:
        amount = int(amount)
    except Exception:
        emit("bj_system", "Invalid bet amount.", to=sid)
        return

    if amount <= 0:
        emit("bj_system", "Bet must be greater than 0.", to=sid)
        return

    game["bet"][sid] = amount
    emit("bj_system", f"Bet set: {amount} Diamonds.", to=room)

    vals = list(game["bet"].values())
    if len(vals) == 2 and len(set(vals)) == 1:
        emit("bj_system", "Bets locked. Click Deal.", to=room)


@socketio.on("bj_chat")
def bj_chat(msg):
    sid = request.sid
    room = bj_sid_to_room.get(sid)
    if not room or room not in bj_rooms:
        emit("bj_system", "You are not in a Blackjack match.")
        return
    
    game = bj_rooms[room]
    p1, p2 = game["players"]
    role = "P1" if sid == p1 else "P2"
    
    # Broadcast the chat message to both players
    socketio.emit("bj_chat", {"role": role, "msg": msg}, to=room)


def _bj_create_deck():
    suits = ["♠", "♥", "♦", "♣"]
    ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    deck = []
    for s in suits:
        for r in ranks:
            v = 11 if r == "A" else 10 if r in ("J", "Q", "K") else int(r)
            deck.append({"r": r, "s": s, "v": v, "label": f"{r}{s}"})
    random.shuffle(deck)
    return deck


def _bj_hand_value(hand):
    total = sum(c["v"] for c in hand)
    aces = sum(1 for c in hand if c["r"] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


@socketio.on("bj_deal")
def bj_deal():
    sid = request.sid
    room = bj_sid_to_room.get(sid)
    if not room or room not in bj_rooms:
        emit("bj_system", "You are not in a Blackjack match.")
        return

    game = bj_rooms[room]
    if game.get("finished"):
        emit("bj_system", "Match is over. Queue again to play.", to=sid)
        return

    # Require locked bets
    vals = list(game.get("bet", {}).values())
    if len(vals) < 2 or len(set(vals)) != 1:
        emit("bj_system", "Both players must set the same bet before dealing.", to=sid)
        return

    if game["in_round"]:
        emit("bj_system", "Round already in progress.", to=sid)
        return

    p1, p2 = game["players"]
    game["deck"] = _bj_create_deck()
    game["hands"] = {p1: [game["deck"].pop(), game["deck"].pop()],
                     p2: [game["deck"].pop(), game["deck"].pop()]}
    game["done"] = {p1: False, p2: False}
    game["active"] = p1
    game["in_round"] = True

    socketio.emit("bj_state", {
        "active": "P1",
        "p1": [c["label"] for c in game["hands"][p1]],
        "p2": [c["label"] for c in game["hands"][p2]],
        "p1v": _bj_hand_value(game["hands"][p1]),
        "p2v": _bj_hand_value(game["hands"][p2]),
        "bet": vals[0],
        "in_round": True,
    }, to=room)

    emit("bj_system", "Cards dealt. P1 acts first.", to=room)


@socketio.on("bj_hit")
def bj_hit():
    sid = request.sid
    room = bj_sid_to_room.get(sid)
    if not room or room not in bj_rooms:
        emit("bj_system", "You are not in a Blackjack match.")
        return

    game = bj_rooms[room]
    if not game["in_round"]:
        emit("bj_system", "No active round. Click Deal.", to=sid)
        return

    if sid != game["active"]:
        emit("bj_system", "Not your turn.", to=sid)
        return

    if not game["deck"]:
        game["deck"] = _bj_create_deck()

    game["hands"][sid].append(game["deck"].pop())
    total = _bj_hand_value(game["hands"][sid])

    # Bust -> mark done and switch
    if total > 21:
        game["done"][sid] = True

    # Switch to the other player if possible
    p1, p2 = game["players"]
    other = p2 if sid == p1 else p1
    if not game["done"].get(other, False):
        game["active"] = other
    else:
        game["active"] = sid  # other is done, keep here

    # If both done -> finish
    if game["done"][p1] and game["done"][p2]:
        bj_finish(room)
        return

    socketio.emit("bj_state", {
        "active": "P1" if game["active"] == p1 else "P2",
        "p1": [c["label"] for c in game["hands"][p1]],
        "p2": [c["label"] for c in game["hands"][p2]],
        "p1v": _bj_hand_value(game["hands"][p1]),
        "p2v": _bj_hand_value(game["hands"][p2]),
        "bet": list(game["bet"].values())[0],
        "in_round": True,
    }, to=room)


@socketio.on("bj_stand")
def bj_stand():
    sid = request.sid
    room = bj_sid_to_room.get(sid)
    if not room or room not in bj_rooms:
        emit("bj_system", "You are not in a Blackjack match.")
        return

    game = bj_rooms[room]
    if not game["in_round"]:
        emit("bj_system", "No active round. Click Deal.", to=sid)
        return

    if sid != game["active"]:
        emit("bj_system", "Not your turn.", to=sid)
        return

    game["done"][sid] = True

    p1, p2 = game["players"]
    other = p2 if sid == p1 else p1
    if not game["done"].get(other, False):
        game["active"] = other

    if game["done"][p1] and game["done"][p2]:
        bj_finish(room)
        return

    socketio.emit("bj_state", {
        "active": "P1" if game["active"] == p1 else "P2",
        "p1": [c["label"] for c in game["hands"][p1]],
        "p2": [c["label"] for c in game["hands"][p2]],
        "p1v": _bj_hand_value(game["hands"][p1]),
        "p2v": _bj_hand_value(game["hands"][p2]),
        "bet": list(game["bet"].values())[0],
        "in_round": True,
    }, to=room)


def bj_finish(room):
    game = bj_rooms.get(room)
    if not game:
        return

    p1, p2 = game["players"]
    p1v = _bj_hand_value(game["hands"][p1])
    p2v = _bj_hand_value(game["hands"][p2])
    bet = list(game["bet"].values())[0]

    def score(v):  # bust -> 0
        return 0 if v > 21 else v

    s1, s2 = score(p1v), score(p2v)

    winner = None
    reason = ""
    
    # Determine winner and reason
    if p1v > 21 and p2v > 21:
        # Both bust
        reason = "Both players bust!"
        winner = None
    elif p1v > 21:
        # P1 busts, P2 wins
        reason = f"P1 busts with {p1v}. P2 wins!"
        winner = "P2"
    elif p2v > 21:
        # P2 busts, P1 wins
        reason = f"P2 busts with {p2v}. P1 wins!"
        winner = "P1"
    elif s1 > s2:
        # P1 has higher score
        reason = f"P1 ({p1v}) beats P2 ({p2v})."
        winner = "P1"
    elif s2 > s1:
        # P2 has higher score
        reason = f"P2 ({p2v}) beats P1 ({p1v})."
        winner = "P2"
    else:
        # Tie
        reason = f"Push at {p1v}."
        winner = None

    # Send the result with reason
    emit("bj_system", reason, to=room)
    socketio.emit("bj_result", {"winner": winner, "bet": bet, "p1v": p1v, "p2v": p2v}, to=room)
    game["in_round"] = False
    game["finished"] = True
    emit("bj_system", "Round finished. Queue again for a new opponent.", to=room)



if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
