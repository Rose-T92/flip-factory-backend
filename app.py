from flask import Flask, request, jsonify, Response, abort
from datetime import datetime, timedelta
import sqlite3
import os
import csv

app = Flask(__name__)
DB_PATH = "database.db"
CSV_LOG_PATH = "redemptions.csv"
MAX_MONTHLY_COINS = 50_000_000  # $50.00 per month
API_KEY = os.getenv("API_KEY", "supersecret123")  # Store in Render secrets in production

def require_api_key():
    key = request.headers.get("X-API-Key")
    if key != API_KEY:
        abort(403)

# Initialize DB if not exists
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            monthly_coin_earned INTEGER DEFAULT 0,
            monthly_coin_redeemed INTEGER DEFAULT 0,
            last_reset TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            coins_redeemed INTEGER,
            usd_value REAL,
            requested_at TEXT,
            status TEXT DEFAULT 'pending'
        )
        """)
        conn.commit()

# Create log entry in CSV
def log_redemption_csv(user_id, usd_value, coins_redeemed, status="pending"):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, user_id, f"{usd_value:.2f}", coins_redeemed, status]
    file_exists = os.path.isfile(CSV_LOG_PATH)
    with open(CSV_LOG_PATH, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["timestamp", "user_id", "usd_value", "coins_redeemed", "status"])
        writer.writerow(row)

if not os.path.exists(DB_PATH):
    init_db()

def get_or_create_user(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            cursor.execute("INSERT INTO users (user_id, last_reset) VALUES (?, ?)", (user_id, today))
            conn.commit()
            return {"user_id": user_id, "monthly_coin_earned": 0, "monthly_coin_redeemed": 0, "last_reset": today}
        return {
            "user_id": user[0],
            "monthly_coin_earned": user[1],
            "monthly_coin_redeemed": user[2],
            "last_reset": user[3]
        }

@app.route("/")
def home():
    return "Flip Factory Backend is running!"

@app.route("/api/coin/earn", methods=["POST"])
def coin_earn():
    require_api_key()
    data = request.json
    user_id = data.get("user_id")
    coins = int(data.get("coins", 0))
    if not user_id or coins <= 0:
        return jsonify({"error": "Invalid request"}), 400

    get_or_create_user(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET monthly_coin_earned = monthly_coin_earned + ? WHERE user_id = ?", (coins, user_id))
        conn.commit()

    return jsonify({"success": True, "earned": coins})
@app.route("/api/coin/redeem", methods=["POST"])
def coin_redeem():
    require_api_key()
    data = request.json
    user_id = data.get("user_id")
    requested_coins = int(data.get("requested_coins", 0))

    if not user_id or requested_coins <= 0:
        return jsonify({"error": "Invalid request"}), 400

    user = get_or_create_user(user_id)
    if user["monthly_coin_redeemed"] + requested_coins > MAX_MONTHLY_COINS:
        return jsonify({"error": "Monthly redeem cap exceeded"}), 403

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET monthly_coin_redeemed = monthly_coin_redeemed + ? WHERE user_id = ?", (requested_coins, user_id))
        conn.commit()

    credited_dollars = requested_coins / 1_000_000
    return jsonify({"success": True, "credited": f"${credited_dollars:.2f}"})

@app.route("/api/coin/exchange", methods=["POST"])
def coin_exchange():
    require_api_key()  # 🔒 Enforce API key check

    data = request.json
    user_id = data.get("user_id")
    usd_requested = float(data.get("usd", 0.0))
    COINS_PER_DOLLAR = 1_000_000
    coins_required = int(usd_requested * COINS_PER_DOLLAR)

    if usd_requested <= 0 or usd_requested > 100:
        return jsonify({"error": "Invalid amount"}), 400

    user = get_or_create_user(user_id)
    available = user["monthly_coin_earned"] - user["monthly_coin_redeemed"]
    if coins_required > available:
        return jsonify({"error": "Insufficient coins"}), 403

    if user["monthly_coin_redeemed"] + coins_required > MAX_MONTHLY_COINS:
        return jsonify({"error": "Monthly redeem cap exceeded"}), 403

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET monthly_coin_redeemed = monthly_coin_redeemed + ? WHERE user_id = ?", (coins_required, user_id))
        cursor.execute("""
            INSERT INTO pending_redemptions (user_id, coins_redeemed, usd_value, requested_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, coins_required, usd_requested, now))
        conn.commit()

    log_redemption_csv(user_id, usd_requested, coins_required)

    return jsonify({"success": True, "usd_requested": f"${usd_requested:.2f}", "expires_in": "24 hours"})


@app.route("/api/redeem/pending", methods=["GET"])
def get_pending_redemptions():
    require_api_key()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, coins_redeemed, usd_value, requested_at, status
            FROM pending_redemptions
            WHERE status = 'pending'
        """)
        rows = cursor.fetchall()
        return jsonify([
            {
                "id": row[0],
                "user_id": row[1],
                "coins": row[2],
                "usd": row[3],
                "requested_at": row[4],
                "status": row[5]
            } for row in rows
        ])

@app.route("/api/redeem/mark_paid", methods=["POST"])
def mark_redeem_paid():
    require_api_key()
    data = request.json
    redemption_id = data.get("id")

    if not redemption_id:
        return jsonify({"error": "Missing redemption ID"}), 400

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE pending_redemptions SET status = 'paid' WHERE id = ?", (redemption_id,))
        conn.commit()

    return jsonify({"success": True, "id": redemption_id})

@app.route("/api/redeem/export_csv", methods=["GET"])
def export_csv():
    require_api_key()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, coins_redeemed, usd_value, requested_at, status
            FROM pending_redemptions
        """)
        rows = cursor.fetchall()

    csv_data = "id,user_id,coins_redeemed,usd_value,requested_at,status\n"
    for row in rows:
        csv_data += ",".join(map(str, row)) + "\n"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=redemptions.csv"}
    )

@app.route("/api/redeem/expire_old", methods=["POST"])
def expire_old_redemptions():
    require_api_key()
    expired_time = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pending_redemptions
            SET status = 'expired'
            WHERE status = 'pending' AND requested_at < ?
        """, (expired_time,))
        conn.commit()

    return jsonify({"success": True, "expired_before": expired_time})

@app.route("/api/coin/status", methods=["GET"])
def coin_status():
    require_api_key()  # 🔒 Enforce API key access

    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    user = get_or_create_user(user_id)
    remaining = max(0, MAX_MONTHLY_COINS - user["monthly_coin_redeemed"])
    return jsonify({
        "user_id": user["user_id"],
        "earned": user["monthly_coin_earned"],
        "redeemed": user["monthly_coin_redeemed"],
        "remaining_redeemable_coins": remaining,
        "dollar_value_redeemed": f"${user['monthly_coin_redeemed'] / 1_000_000:.2f}"
    })



@app.route("/api/coin/reset_monthly", methods=["POST"])
def reset_monthly():
    require_api_key()  # 🔒 Enforce API key check
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET monthly_coin_earned = 0, monthly_coin_redeemed = 0, last_reset = ?
        """, (today,))
        conn.commit()
    return jsonify({"success": True, "message": "Monthly stats reset"})

@app.route("/api/export/redemptions", methods=["GET"])
def export_redemptions_csv():
    require_api_key()  # 🔒 Enforce API key check

    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)

    writer.writerow(["id", "user_id", "coins_redeemed", "usd_value", "requested_at", "status"])

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, coins_redeemed, usd_value, requested_at, status FROM pending_redemptions")
        for row in cursor.fetchall():
            writer.writerow(row)

    output.seek(0)
    return app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=redemptions_export.csv"}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

