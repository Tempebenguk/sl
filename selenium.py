import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading
import psutil
from flask_cors import CORS

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# 📌 Konfigurasi transaksi
TIMEOUT = 180
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 2  # 🔥 Maksimal ulang 2 kali
DEVICE_ID = "ba001"  # 🔥 Variabel untuk identifikasi device

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# 📌 API URL
INVOICE_API = "http://172.16.100.160:5000/invoice/device/"
DETAIL_API = "http://172.16.100.160:5000/invoice/token/"
BILL_API = "http://172.16.100.160:5000/payment"

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "/home/eksan/logging/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

# 📌 Inisialisasi Flask
app = Flask(__name__)
CORS(app)

# 📌 Variabel Global
transaction_active = False
total_inserted = 0
id_trx = None
payment_token = None
product_price = 0
last_pulse_received_time = time.time()
timeout_thread = None
insufficient_payment_count = 0

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def fetch_invoice_token():
    """Mendapatkan payment token dari server Flask."""
    try:
        response = requests.get(f"{INVOICE_API}{DEVICE_ID}", timeout=5)
        if response.status_code == 200:
            return response.json().get("paymentToken")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil token invoice: {e}")
    return None

def fetch_invoice_details(payment_token):
    """Mendapatkan detail invoice berdasarkan token."""
    try:
        response = requests.get(f"{DETAIL_API}{payment_token}", timeout=5)
        if response.status_code == 200:
            invoice_data = response.json().get("data")
            created_at = datetime.datetime.fromisoformat(invoice_data["CreatedAt"])
            if (datetime.datetime.utcnow() - created_at).total_seconds() <= 180:
                return invoice_data["ID"], invoice_data["paymentToken"], int(invoice_data["productPrice"])
            else:
                log_transaction("🚫 Transaksi kadaluarsa (>3 menit)")
        else:
            log_transaction("🚫 Token tidak valid")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")
    return None, None, None

def send_transaction_status():
    """Mengirim hasil transaksi setelah selesai."""
    try:
        response = requests.post(BILL_API, json={
            "id": id_trx,
            "paymentToken": payment_token,
            "productPrice": total_inserted
        }, timeout=5)
        if response.status_code == 200:
            log_transaction("✅ Pembayaran sukses")
        else:
            log_transaction(f"⚠️ Gagal mengirim transaksi: {response.text}")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim transaksi: {e}")

def transaction_loop():
    """Loop utama untuk mengecek transaksi setiap detik."""
    global transaction_active, id_trx, payment_token, product_price
    while True:
        if not transaction_active:
            token = fetch_invoice_token()
            if token:
                id_trx, payment_token, product_price = fetch_invoice_details(token)
                if id_trx and product_price:
                    transaction_active = True
                    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")
                    pi.write(EN_PIN, 1)
        time.sleep(1)

threading.Thread(target=transaction_loop, daemon=True).start()

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, lambda gpio, level, tick: None)
    app.run(host="0.0.0.0", port=5000, debug=True)
