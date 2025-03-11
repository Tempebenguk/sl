import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# 📌 Konfigurasi transaksi
TIMEOUT = 180
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 2  # 🔥 Maksimal ulang 2 kali

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
SERVER_API = "https://api-dev.xpdisi.id/"
INVOICE_API = f"{SERVER_API}/invoice/device/"
TOKEN_API = f"{SERVER_API}/invoice/"
BILL_API = f"{SERVER_API}/order/billacceptor/"

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
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

# 📌 Variabel Global
pulse_count = 0
pending_pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
total_inserted = 0
id_trx = None
payment_token = None
product_price = 0
last_pulse_received_time = time.time()
timeout_thread = None
insufficient_payment_count = 0
device_id = "bic01"  # 🔥 Gantilah sesuai identifikasi device

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def get_latest_payment_token():
    """Mengambil payment token terbaru dari server."""
    try:
        response = requests.get(f"{INVOICE_API}{device_id}", timeout=5)
        data = response.json()
        if response.status_code == 200 and "data" in data and data["data"]:
            latest_invoice = data["data"][0]  # Ambil invoice terbaru
            created_at = datetime.datetime.fromisoformat(latest_invoice["CreatedAt"][:-1])
            payment_token = latest_invoice["PaymentToken"]
            
            # Cek apakah waktu transaksi masih dalam 3 menit terakhir
            if (datetime.datetime.utcnow() - created_at).total_seconds() <= 180:
                return payment_token
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil payment token: {e}")
    return None

def fetch_invoice_details(payment_token):
    """Mengambil detail invoice berdasarkan token pembayaran."""
    try:
        response = requests.get(f"{TOKEN_API}{payment_token}", timeout=5)
        response_data = response.json()
        if response.status_code == 200 and "data" in response_data:
            invoice_data = response_data["data"]
            if not invoice_data.get("isPaid", False):
                return invoice_data["ID"], invoice_data["paymentToken"], int(invoice_data["productPrice"])
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")
    return None, None, None

def activate_bill_acceptor():
    """Mengaktifkan bill acceptor berdasarkan token pembayaran yang valid."""
    global transaction_active, id_trx, payment_token, product_price, last_pulse_received_time
    while True:
        if not transaction_active:
            payment_token = get_latest_payment_token()
            if payment_token:
                id_trx, payment_token, product_price = fetch_invoice_details(payment_token)
                if id_trx:
                    transaction_active = True
                    last_pulse_received_time = time.time()
                    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")
                    pi.write(EN_PIN, 1)
                    threading.Thread(target=start_timeout_timer, daemon=True).start()
        time.sleep(1)

def send_transaction_status():
    """Mengirim hasil transaksi ke server."""
    global total_inserted, transaction_active
    try:
        response = requests.post(BILL_API, json={
            "id": id_trx,
            "paymentToken": payment_token,
            "productPrice": total_inserted
        }, timeout=5)
        if response.status_code == 200:
            log_transaction(f"✅ Pembayaran sukses: {response.json().get('message')}")
        reset_transaction()
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")

def reset_transaction():
    """Mengatur ulang transaksi setelah selesai."""
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time
    transaction_active = False
    total_inserted = 0
    id_trx = None
    payment_token = None
    product_price = 0
    last_pulse_received_time = time.time()
    log_transaction("🔄 Transaksi di-reset ke default.")

def start_timeout_timer():
    """Mengatur timer timeout transaksi."""
    global transaction_active
    while transaction_active:
        if (time.time() - last_pulse_received_time) >= TIMEOUT:
            transaction_active = False
            pi.write(EN_PIN, 0)
            log_transaction("⏰ Timeout! Transaksi dibatalkan.")
            reset_transaction()
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=activate_bill_acceptor, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
