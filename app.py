import os
import sqlite3
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request
import requests
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

DB_PATH = os.environ.get("SQLITE_DB_PATH", "trading.db")
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
COINGECKO_MAP = {
    "bitcoin": "BTC/USDT",
    "ethereum": "ETH/USDT",
    "solana": "SOL/USDT",
    "binancecoin": "BNB/USDT",
}

LWS_API_URL = os.environ.get("LWS_API_URL", "https://kingpronos.net/trading/api_db.php").strip()
LWS_SYNC_TOKEN = os.environ.get("LWS_SYNC_TOKEN", "").strip()

state_lock = threading.Lock()
bot_state = {
    "mode": os.environ.get("TRADING_MODE", "SIMULATION").upper(),
    "running": True,
    "capital_simule": 1000.0,
    "montant_par_trade": 50.0,
    "api_key": os.environ.get("EXCHANGE_API_KEY", ""),
    "api_secret": os.environ.get("EXCHANGE_API_SECRET", ""),
    "last_scan": None,
    "last_error": None,
    "lws_sync_status": "En attente",
}


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS marche_live (
                paire TEXT PRIMARY KEY,
                prix REAL,
                variation_24h REAL,
                signal TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                remote_id INTEGER,
                date_cycle TEXT,
                mode TEXT,
                paire TEXT,
                prix_entree REAL,
                prix_sortie REAL,
                quantite REAL,
                profit_usdt REAL,
                statut TEXT
            )
            """
        )
        cur.execute("PRAGMA table_info(cycles)")
        columns = [row[1] for row in cur.fetchall()]
        if "remote_id" not in columns:
            cur.execute("ALTER TABLE cycles ADD COLUMN remote_id INTEGER")
        conn.commit()


def sync_to_lws(payload: dict):
    """Envoie un événement vers api_db.php sur LWS."""
    if not LWS_API_URL or not LWS_SYNC_TOKEN:
        with state_lock:
            bot_state["lws_sync_status"] = "Non configuré"
        return {"ok": False, "error": "LWS_API_URL ou LWS_SYNC_TOKEN manquant"}

    body = dict(payload)
    body["token"] = LWS_SYNC_TOKEN

    try:
        resp = requests.post(LWS_API_URL, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        if data.get("error"):
            with state_lock:
                bot_state["lws_sync_status"] = f"Erreur PHP: {data.get('error')}"
            return data
        with state_lock:
            bot_state["lws_sync_status"] = f"OK ({datetime.now().strftime('%H:%M:%S')})"
        return data
    except Exception as e:
        with state_lock:
            bot_state["lws_sync_status"] = f"Erreur: {e}"
        print(f"[SYNC LWS ERREUR] {e}", flush=True)
        return {"ok": False, "error": str(e)}


def fetch_lws_trades():
    """Récupère les derniers trades depuis MySQL LWS si configuré."""
    res = sync_to_lws({"action": "list_trades"})
    if isinstance(res, dict) and res.get("ok") and isinstance(res.get("trades"), list):
        formatted = []
        for row in res["trades"]:
            formatted.append(
                {
                    "id": row.get("id"),
                    "date_cycle": row.get("date_cycle"),
                    "mode": row.get("mode"),
                    "paire": row.get("paire"),
                    "prix_entree": row.get("prix_entree"),
                    "prix_sortie": row.get("prix_sortie"),
                    "quantite": row.get("quantite"),
                    "profit_usdt": row.get("profit_usdt"),
                    "statut": row.get("statut"),
                }
            )
        return formatted
    return None


def get_exchange(api_key="", api_secret=""):
    opts = {
        "enableRateLimit": True,
        "timeout": 10000,
    }
    if api_key and api_secret:
        opts["apiKey"] = api_key
        opts["secret"] = api_secret
    return ccxt.binance(opts)


def compute_signal(change_pct: float) -> str:
    if change_pct is None:
        return "NEUTRE"
    if change_pct >= 1.5:
        return "ACHAT FORT"
    if change_pct <= -1.5:
        return "VENTE / PRUDENCE"
    return "NEUTRE"


def fetch_market_coingecko():
    """Repli public via CoinGecko si Binance est indisponible ou bloqué sur Render."""
    ids = ",".join(COINGECKO_MAP.keys())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    out = {}
    for cg_id, sym in COINGECKO_MAP.items():
        item = payload.get(cg_id)
        if item and item.get("usd") is not None:
            price = float(item["usd"])
            pct = float(item.get("usd_24h_change") or 0.0)
            out[sym] = (price, pct)
    return out


def fetch_market_snapshot(exchange):
    """Récupère les tickers via CCXT Binance, avec repli automatique CoinGecko si nécessaire."""
    results = {}
    errors = []

    try:
        tickers = exchange.fetch_tickers(SYMBOLS)
        if isinstance(tickers, dict) and tickers:
            for sym in SYMBOLS:
                t = tickers.get(sym)
                if t and t.get("last") is not None:
                    price = float(t["last"])
                    pct = float(t["percentage"]) if t.get("percentage") is not None else 0.0
                    results[sym] = (price, pct)
    except Exception as e:
        errors.append(f"Binance: {e}")

    for sym in SYMBOLS:
        if sym in results:
            continue
        try:
            t = exchange.fetch_ticker(sym)
            if t and t.get("last") is not None:
                price = float(t["last"])
                pct = float(t["percentage"]) if t.get("percentage") is not None else 0.0
                results[sym] = (price, pct)
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if not results:
        try:
            cg_data = fetch_market_coingecko()
            if cg_data:
                results.update(cg_data)
                errors = [f"Repli CoinGecko actif ({'; '.join(errors)})"] if errors else []
        except Exception as cg_err:
            errors.append(f"CoinGecko: {cg_err}")

    return results, errors


def update_market_table(snapshot: dict, now_str: str):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        for sym, (price, pct) in snapshot.items():
            sig = compute_signal(pct)
            cur.execute(
                """
                INSERT INTO marche_live (paire, prix, variation_24h, signal, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(paire) DO UPDATE SET
                    prix = excluded.prix,
                    variation_24h = excluded.variation_24h,
                    signal = excluded.signal,
                    updated_at = excluded.updated_at
                """,
                (sym, price, pct, sig, now_str),
            )
        conn.commit()


def process_trading_logic(snapshot: dict, now_str: str):
    with state_lock:
        mode = bot_state["mode"]
        running = bot_state["running"]
        montant = bot_state["montant_par_trade"]
        api_key = bot_state["api_key"]
        api_secret = bot_state["api_secret"]

    if not running:
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT * FROM cycles WHERE statut = 'OUVERT'")
        open_trades = cur.fetchall()

        for trade in open_trades:
            sym = trade["paire"]
            if sym not in snapshot:
                continue
            current_price, _ = snapshot[sym]
            entry_price = float(trade["prix_entree"])
            qty = float(trade["quantite"])

            if entry_price <= 0:
                continue

            pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
            if pnl_pct >= 0.8 or pnl_pct <= -0.6:
                profit_usdt = round((current_price - entry_price) * qty, 4)
                statut_clos = "FERME (SIMULATION)" if trade["mode"] == "SIMULATION" else "FERME (REEL)"

                cur.execute(
                    """
                    UPDATE cycles
                    SET prix_sortie = ?, profit_usdt = ?, statut = ?
                    WHERE id = ?
                    """,
                    (current_price, profit_usdt, statut_clos, trade["id"]),
                )

                if trade["mode"] == "SIMULATION":
                    with state_lock:
                        bot_state["capital_simule"] = round(
                            bot_state["capital_simule"] + (current_price * qty), 4
                        )

                remote_id = trade["remote_id"]
                if remote_id:
                    sync_to_lws(
                        {
                            "action": "close_trade",
                            "remote_id": remote_id,
                            "prix_sortie": current_price,
                            "profit_usdt": profit_usdt,
                            "statut": statut_clos,
                        }
                    )

        for sym, (price, pct) in snapshot.items():
            if compute_signal(pct) != "ACHAT FORT" or price <= 0:
                continue

            cur.execute("SELECT COUNT(*) AS cnt FROM cycles WHERE paire = ? AND statut = 'OUVERT'", (sym,))
            if cur.fetchone()["cnt"] > 0:
                continue

            qty = round(montant / price, 6)
            if qty <= 0:
                continue

            if mode == "SIMULATION":
                with state_lock:
                    if bot_state["capital_simule"] < montant:
                        continue
                    bot_state["capital_simule"] = round(bot_state["capital_simule"] - montant, 4)

                cur.execute(
                    """
                    INSERT INTO cycles
                    (remote_id, date_cycle, mode, paire, prix_entree, prix_sortie, quantite, profit_usdt, statut)
                    VALUES (NULL, ?, ?, ?, ?, NULL, ?, NULL, 'OUVERT')
                    """,
                    (now_str, mode, sym, price, qty),
                )
                local_id = cur.lastrowid

                lws_res = sync_to_lws(
                    {
                        "action": "insert_trade",
                        "date_cycle": now_str,
                        "mode": mode,
                        "paire": sym,
                        "prix_entree": price,
                        "prix_sortie": None,
                        "quantite": qty,
                        "profit_usdt": None,
                        "statut": "OUVERT",
                    }
                )
                if isinstance(lws_res, dict) and lws_res.get("id"):
                    cur.execute(
                        "UPDATE cycles SET remote_id = ? WHERE id = ?",
                        (int(lws_res["id"]), local_id),
                    )

            elif mode == "RÉEL" and api_key and api_secret:
                try:
                    ex_real = get_exchange(api_key, api_secret)
                    order = ex_real.create_market_buy_order(sym, qty)
                    exec_price = float(order.get("average") or order.get("price") or price)
                    exec_qty = float(order.get("filled") or qty)

                    cur.execute(
                        """
                        INSERT INTO cycles
                        (remote_id, date_cycle, mode, paire, prix_entree, prix_sortie, quantite, profit_usdt, statut)
                        VALUES (NULL, ?, 'REEL', ?, ?, NULL, ?, NULL, 'OUVERT')
                        """,
                        (now_str, sym, exec_price, exec_qty),
                    )
                    local_id = cur.lastrowid

                    lws_res = sync_to_lws(
                        {
                            "action": "insert_trade",
                            "date_cycle": now_str,
                            "mode": "REEL",
                            "paire": sym,
                            "prix_entree": exec_price,
                            "prix_sortie": None,
                            "quantite": exec_qty,
                            "profit_usdt": None,
                            "statut": "OUVERT",
                        }
                    )
                    if isinstance(lws_res, dict) and lws_res.get("id"):
                        cur.execute(
                            "UPDATE cycles SET remote_id = ? WHERE id = ?",
                            (int(lws_res["id"]), local_id),
                        )
                except Exception as e:
                    with state_lock:
                        bot_state["last_error"] = f"Ordre réel {sym}: {e}"
                    print(f"[ORDRE REEL ERREUR] {sym}: {e}", flush=True)

        conn.commit()


def run_single_cycle(exchange=None):
    if exchange is None:
        exchange = get_exchange()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot, errors = fetch_market_snapshot(exchange)

    if snapshot:
        update_market_table(snapshot, now_str)
        process_trading_logic(snapshot, now_str)

    with state_lock:
        bot_state["last_scan"] = now_str
        bot_state["last_error"] = " | ".join(errors) if errors else None


def bot_loop():
    init_db()
    exchange = get_exchange()

    try:
        run_single_cycle(exchange)
    except Exception as e:
        with state_lock:
            bot_state["last_error"] = str(e)
            bot_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[ERREUR BOT INIT] {e}", flush=True)

    while True:
        time.sleep(20)
        try:
            run_single_cycle(exchange)
        except Exception as e:
            with state_lock:
                bot_state["last_error"] = str(e)
            print(f"[ERREUR BOT] {e}", flush=True)


_thread_started = False
_thread_lock = threading.Lock()


def ensure_background_bot():
    global _thread_started
    with _thread_lock:
        if not _thread_started:
            init_db()
            t = threading.Thread(target=bot_loop, daemon=True, name="TradingBotThread")
            t.start()
            _thread_started = True


@app.before_request
def _before_any_request():
    ensure_background_bot()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Bot Trading Cloud (Simulation & Réel)</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%231e293b'/%3E%3Cpath d='M16 40l11-11 8 7 13-15' fill='none' stroke='%2322c55e' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='48' cy='21' r='4' fill='%2338bdf8'/%3E%3C/svg%3E" />
  <style>
    * { box-sizing: border-box; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
    body { margin: 0; background: #eef2f6; color: #0f172a; padding: 20px; }
    .container { max-width: 1120px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 4px 18px rgba(15, 23, 42, 0.08); }
    .header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { margin: 0; font-size: 24px; }
    .meta-bar { font-size: 12px; color: #475569; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 18px; }
    .card { border: 1px solid #e2e8f0; border-left: 4px solid #2563eb; border-radius: 8px; padding: 14px; background: #f8fafc; }
    .card-title { font-size: 13px; font-weight: 700; color: #334155; margin-bottom: 8px; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 6px; font-weight: 700; font-size: 12px; color: #fff; margin-bottom: 8px; }
    .badge-sim { background: #d97706; }
    .badge-real { background: #dc2626; }
    .badge-on { background: #16a34a; }
    .badge-off { background: #64748b; }
    button { border: none; border-radius: 6px; padding: 8px 14px; font-weight: 600; cursor: pointer; font-size: 13px; }
    .btn-sm { padding: 5px 10px; font-size: 12px; }
    .btn-red { background: #dc2626; color: #fff; }
    .btn-green { background: #16a34a; color: #fff; }
    .btn-dark { background: #1e293b; color: #fff; }
    .api-box { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 22px; }
    .api-form { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
    input[type="text"], input[type="password"] { flex: 1; min-width: 220px; padding: 9px 12px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px; }
    h2 { font-size: 18px; margin: 20px 0 10px; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 18px; font-size: 13px; }
    th, td { border-bottom: 1px solid #e2e8f0; padding: 10px 12px; text-align: left; }
    th { background: #f1f5f9; font-weight: 700; color: #1e293b; }
    .err-banner { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 14px; display: none; }
    .info-banner { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a; padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 14px; display: none; }
    .pos { color: #15803d; font-weight: 700; }
    .neg { color: #b91c1c; font-weight: 700; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <svg width="36" height="36" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
          <rect width="64" height="64" rx="14" fill="#1e293b"/>
          <path d="M16 40l11-11 8 7 13-15" fill="none" stroke="#22c55e" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="48" cy="21" r="4" fill="#38bdf8"/>
        </svg>
        <h1>Bot Trading Cloud (Simulation &amp; Réel)</h1>
      </div>
      <div class="meta-bar">
        <span id="lastScan">Dernier scan : --</span>
        <span id="lwsStatus">MySQL LWS : --</span>
        <button class="btn-dark btn-sm" onclick="testLws()">Tester MySQL LWS</button>
      </div>
    </div>

    <div id="infoBanner" class="info-banner"></div>
    <div id="errBanner" class="err-banner"></div>

    <div class="grid-4">
      <div class="card">
        <div class="card-title">Mode Actuel</div>
        <div id="modeBadge" class="badge badge-sim">SIMULATION</div><br/>
        <button id="toggleModeBtn" class="btn-red" onclick="toggleMode()">Passer en RÉEL</button>
      </div>
      <div class="card">
        <div class="card-title">État du Moteur</div>
        <div id="runBadge" class="badge badge-on">ACTIF</div><br/>
        <button id="toggleRunBtn" class="btn-dark" onclick="toggleBot()">Arrêter</button>
      </div>
      <div class="card">
        <div class="card-title">Capital Simulé</div>
        <div id="capitalVal" style="font-size:20px;font-weight:800;margin-bottom:4px;">$1000.00 USDT</div>
        <div id="miseVal" style="font-size:12px;color:#475569;">Mise / trade : $50.00</div>
      </div>
      <div class="card">
        <div class="card-title">Clés API Exchange</div>
        <div id="apiStatus" style="font-size:13px;color:#334155;">Non renseignées (Simulation uniquement)</div>
      </div>
    </div>

    <div class="api-box">
      <div class="card-title" style="margin-bottom:0;">Configuration API pour le Mode Réel</div>
      <div class="api-form">
        <input type="text" id="apiKeyInput" placeholder="Exchange API Key" />
        <input type="password" id="apiSecretInput" placeholder="Exchange Secret Key" />
        <button class="btn-dark" onclick="saveConfig()">Enregistrer les clés en mémoire</button>
      </div>
    </div>

    <h2>Marché &amp; Signaux en Direct</h2>
    <table>
      <thead>
        <tr>
          <th>Paire</th>
          <th>Prix (USDT)</th>
          <th>Variation 24h</th>
          <th>Signal</th>
          <th>Mise à jour</th>
        </tr>
      </thead>
      <tbody id="marketBody">
        <tr><td colspan="5">Initialisation du flux marché...</td></tr>
      </tbody>
    </table>

    <h2>Historique des Cycles (Achat &amp; Vente)</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Date</th>
          <th>Mode</th>
          <th>Paire</th>
          <th>Entrée</th>
          <th>Sortie</th>
          <th>Quantité</th>
          <th>Profit (USDT)</th>
          <th>Statut</th>
        </tr>
      </thead>
      <tbody id="cyclesBody">
        <tr><td colspan="9">Aucun cycle enregistré pour l'instant.</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();

        const isReal = data.mode === 'RÉEL' || data.mode === 'REEL';
        const modeBadge = document.getElementById('modeBadge');
        modeBadge.textContent = data.mode;
        modeBadge.className = 'badge ' + (isReal ? 'badge-real' : 'badge-sim');

        const toggleModeBtn = document.getElementById('toggleModeBtn');
        toggleModeBtn.textContent = isReal ? 'Passer en SIMULATION' : 'Passer en RÉEL';
        toggleModeBtn.className = isReal ? 'btn-green' : 'btn-red';

        const runBadge = document.getElementById('runBadge');
        runBadge.textContent = data.running ? 'ACTIF' : 'ARRÊTÉ';
        runBadge.className = 'badge ' + (data.running ? 'badge-on' : 'badge-off');
        document.getElementById('toggleRunBtn').textContent = data.running ? 'Arrêter' : 'Démarrer';

        document.getElementById('capitalVal').textContent = '$' + Number(data.capital_simule).toFixed(2) + ' USDT';
        document.getElementById('miseVal').textContent = 'Mise / trade : $' + Number(data.montant_par_trade).toFixed(2);
        document.getElementById('apiStatus').textContent = data.has_api_keys
          ? 'Clés API configurées en mémoire'
          : 'Non renseignées (Simulation uniquement)';

        document.getElementById('lastScan').textContent = 'Dernier scan : ' + (data.last_scan || '--');
        document.getElementById('lwsStatus').textContent = 'MySQL LWS : ' + (data.lws_sync_status || '--');

        const errBanner = document.getElementById('errBanner');
        if (data.last_error) {
          errBanner.style.display = 'block';
          errBanner.textContent = 'Alerte flux : ' + data.last_error;
        } else {
          errBanner.style.display = 'none';
        }

        const marketBody = document.getElementById('marketBody');
        if (data.market && data.market.length > 0) {
          marketBody.innerHTML = data.market.map(m => {
            const pct = Number(m.variation_24h || 0);
            const pctClass = pct >= 0 ? 'pos' : 'neg';
            const sign = pct >= 0 ? '+' : '';
            return `<tr>
              <td><strong>${m.paire}</strong></td>
              <td>${Number(m.prix).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}</td>
              <td class="${pctClass}">${sign}${pct.toFixed(2)}%</td>
              <td>${m.signal}</td>
              <td>${m.updated_at || ''}</td>
            </tr>`;
          }).join('');
        } else {
          marketBody.innerHTML = '<tr><td colspan="5">Initialisation du flux marché...</td></tr>';
        }

        const cyclesBody = document.getElementById('cyclesBody');
        if (data.cycles && data.cycles.length > 0) {
          cyclesBody.innerHTML = data.cycles.map(c => {
            const profit = c.profit_usdt === null || c.profit_usdt === undefined ? '-' : Number(c.profit_usdt).toFixed(4);
            const sortie = c.prix_sortie === null || c.prix_sortie === undefined ? '-' : Number(c.prix_sortie).toFixed(4);
            return `<tr>
              <td>${c.id}</td>
              <td>${c.date_cycle}</td>
              <td>${c.mode}</td>
              <td>${c.paire}</td>
              <td>${Number(c.prix_entree).toFixed(4)}</td>
              <td>${sortie}</td>
              <td>${Number(c.quantite).toFixed(6)}</td>
              <td>${profit}</td>
              <td>${c.statut}</td>
            </tr>`;
          }).join('');
        } else {
          cyclesBody.innerHTML = '<tr><td colspan="9">Aucun cycle enregistré pour l\'instant.</td></tr>';
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function testLws() {
      const infoBanner = document.getElementById('infoBanner');
      infoBanner.style.display = 'block';
      infoBanner.textContent = 'Test de connexion vers MySQL LWS en cours...';
      try {
        const res = await fetch('/api/test-lws');
        const data = await res.json();
        infoBanner.textContent = 'Réponse MySQL LWS : ' + JSON.stringify(data);
      } catch (e) {
        infoBanner.textContent = 'Erreur test LWS : ' + e;
      }
      refreshStatus();
    }

    async function toggleMode() {
      await fetch('/api/mode', { method: 'POST' });
      refreshStatus();
    }

    async function toggleBot() {
      await fetch('/api/bot', { method: 'POST' });
      refreshStatus();
    }

    async function saveConfig() {
      const api_key = document.getElementById('apiKeyInput').value.trim();
      const api_secret = document.getElementById('apiSecretInput').value.trim();
      await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key, api_secret })
      });
      document.getElementById('apiKeyInput').value = '';
      document.getElementById('apiSecretInput').value = '';
      refreshStatus();
    }

    refreshStatus();
    setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    ensure_background_bot()
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/test-lws")
def api_test_lws():
    res = sync_to_lws({"action": "list_trades"})
    return jsonify(res)


@app.route("/api/status")
def api_status():
    ensure_background_bot()
    init_db()

    with state_lock:
        snap_state = dict(bot_state)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT paire, prix, variation_24h, signal, updated_at FROM marche_live")
        market_rows = [dict(r) for r in cur.fetchall()]
        order_map = {sym: idx for idx, sym in enumerate(SYMBOLS)}
        market_rows.sort(key=lambda x: order_map.get(x["paire"], 999))

        cur.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 100")
        local_cycles = [dict(r) for r in cur.fetchall()]

    lws_cycles = fetch_lws_trades() if not local_cycles else None
    cycles_to_show = lws_cycles if (lws_cycles is not None and len(lws_cycles) > 0) else local_cycles

    return jsonify(
        {
            "mode": snap_state["mode"],
            "running": snap_state["running"],
            "capital_simule": snap_state["capital_simule"],
            "montant_par_trade": snap_state["montant_par_trade"],
            "has_api_keys": bool(snap_state["api_key"] and snap_state["api_secret"]),
            "last_scan": snap_state["last_scan"],
            "last_error": snap_state["last_error"],
            "lws_sync_status": snap_state["lws_sync_status"],
            "market": market_rows,
            "cycles": cycles_to_show,
        }
    )


@app.route("/api/mode", methods=["POST"])
def api_mode():
    with state_lock:
        bot_state["mode"] = "RÉEL" if bot_state["mode"] == "SIMULATION" else "SIMULATION"
        current = bot_state["mode"]
    return jsonify({"ok": True, "mode": current})


@app.route("/api/bot", methods=["POST"])
def api_bot():
    with state_lock:
        bot_state["running"] = not bot_state["running"]
        running = bot_state["running"]
    return jsonify({"ok": True, "running": running})


@app.route("/api/config", methods=["POST"])
def api_config():
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    api_secret = (payload.get("api_secret") or "").strip()
    with state_lock:
        if api_key:
            bot_state["api_key"] = api_key
        if api_secret:
            bot_state["api_secret"] = api_secret
    return jsonify({"ok": True, "has_api_keys": bool(bot_state["api_key"] and bot_state["api_secret"])})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)