import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# Konfigurasi transaksi
TIMEOUT = 180
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 2 

# Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# API URL
TOKEN_API = "https://api-dev.xpdisi.id/invoice/device/bic01"
INVOICE_API = "https://api-dev.xpdisi.id/invoice/"
BILL_API = "https://api-dev.xpdisi.id/order/billacceptor"

# Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Inisialisasi Flask
app = Flask(__name__)

# Variabel Global
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
transaction_lock = threading.Lock()
log_lock = threading.Lock()
print_lock = threading.Lock()

# Fungsi log transaction
def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with log_lock:
        with open(LOG_FILE, "a") as log:
            log.write(f"{timestamp} {message}\n")
            
    with print_lock:
        print(f"{timestamp} {message}")

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

# Fungsi GET ke API Invoice
def fetch_invoice_details():
    try:
        response = requests.get(INVOICE_API, timeout=5)
        response_data = response.json()

        if response.status_code == 200 and "data" in response_data:
            for invoice in response_data["data"]:
                if not invoice.get("isPaid", False):
                    log_transaction(f"✅ Invoice ditemukan: {invoice['paymentToken']}, belum dibayar.")
                    return invoice["ID"], invoice["paymentToken"], int(invoice["productPrice"])

        log_transaction("✅ Tidak ada invoice yang belum dibayar.")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")

    return None, None, None

# Fungsi POST hasil transaksi
def send_transaction_status():
    global total_inserted, transaction_active, last_pulse_received_time

    try:
        response = requests.post(BILL_API, json={
            "ID": id_trx,
            "paymentToken": payment_token,
            "productPrice": total_inserted
        }, timeout=5)

        if response.status_code == 200:
            res_data = response.json()
            log_transaction(f"✅ Pembayaran sukses: {res_data.get('message')}, Waktu: {res_data.get('payment date')}")
            reset_transaction() 

        elif response.status_code == 400:
            try:
                res_data = response.json()
                error_message = res_data.get("error") or res_data.get("message", "Error tidak diketahui")
            except ValueError:
                error_message = response.text 

            log_transaction(f"⚠️ Gagal ({response.status_code}): {error_message}")

            if "Insufficient payment" in error_message:
                global insufficient_payment_count
                insufficient_payment_count += 1 

                if insufficient_payment_count > MAX_RETRY:
                    log_transaction("🚫 Pembayaran kurang dan telah melebihi toleransi transaksi, transaksi dibatalkan!")
                    reset_transaction()
                    pi.write(EN_PIN, 1)  
                else:
                    log_transaction(f"🔄 Pembayaran kurang, percobaan {insufficient_payment_count}/{MAX_RETRY}. Lanjutkan memasukkan uang...")
                    last_pulse_received_time = time.time()
                    transaction_active = True 
                    pi.write(EN_PIN, 1) 
                    start_timeout_timer()

            elif "Payment already completed" in error_message:
                log_transaction("✅ Pembayaran sudah selesai sebelumnya. Reset transaksi.")
                pi.write(EN_PIN, 0)  

        else:
            log_transaction(f"⚠️ Respon tidak terduga: {response.status_code}")

    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
    reset_transaction()
        
def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari bill acceptor dan mengonversinya ke nominal uang."""
    global pulse_count, last_pulse_time, total_inserted, last_pulse_received_time, product_price, pending_pulse_count, timeout_thread

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        if pending_pulse_count == 0:
            pi.write(EN_PIN, 0)
        pending_pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time 
        with print_lock:
            print(f"🔢 Pulsa diterima: {pending_pulse_count}")  
        if timeout_thread is None or not timeout_thread.is_alive():
            timeout_thread = threading.Thread(target=start_timeout_timer, daemon=True)
            timeout_thread.start()

# Fungsi untuk menangani timeout & pembayaran sukses
def start_timeout_timer():
    global total_inserted, product_price, transaction_active, last_pulse_received_time, id_trx

    with transaction_lock: 
        while transaction_active:
            current_time = time.time()
            remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time))) 
            if (current_time - last_pulse_received_time) >= 2 and pending_pulse_count > 0:
                    process_final_pulse_count()
                    continue
            if (current_time - last_pulse_received_time) >= 2 and total_inserted >= product_price:
                    transaction_active = False
                    pi.write(EN_PIN, 0)  

                    overpaid = max(0, total_inserted - product_price) 

                    if total_inserted == product_price:
                        log_transaction(f"✅ Transaksi selesai, total: Rp.{total_inserted}")
                    else:
                        log_transaction(f"✅ Transaksi selesai, kelebihan: Rp.{overpaid}")

                    # Kirim status transaksi
                    send_transaction_status()
                    trigger_transaction()
            if remaining_time == 0:
                    # imeout tercapai, hentikan transaksi
                    transaction_active = False
                    pi.write(EN_PIN, 0) 

                    remaining_due = max(0, product_price - total_inserted)
                    overpaid = max(0, total_inserted - product_price) 

                    if total_inserted < product_price:
                        log_transaction(f"⏰ Timeout! Kurang: Rp.{remaining_due}")
                    elif total_inserted == product_price:
                        log_transaction(f"✅ Transaksi sukses, total: Rp.{total_inserted}")
                    else:
                        log_transaction(f"✅ Transaksi sukses, kelebihan: Rp.{overpaid}")

                    send_transaction_status()
                    break 
            with print_lock:    
                print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")
            time.sleep(1)

def process_final_pulse_count():
    """Memproses pulsa yang terkumpul setelah tidak ada pulsa masuk selama 2 detik."""
    global pending_pulse_count, total_inserted, pulse_count

    if pending_pulse_count == 0:
        return

    # Koreksi pulsa dengan toleransi ±2
    corrected_pulses = closest_valid_pulse(pending_pulse_count)

    if corrected_pulses:
        received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
        total_inserted += received_amount
        remaining_due = max(product_price - total_inserted, 0)

        log_transaction(f"💰 Koreksi pulsa: {pending_pulse_count} -> {corrected_pulses} ({received_amount}) | Total: Rp.{total_inserted} | Sisa: Rp.{remaining_due}")
    
    else:
        log_transaction(f"⚠️ Pulsa {pending_pulse_count} tidak valid!")

    pending_pulse_count = 0 
    pi.write(EN_PIN, 1)
    with print_lock:
        print("✅ Koreksi selesai, EN_PIN diaktifkan kembali")

# Reset transaksi setelah selesai
def reset_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time, insufficient_payment_count, pending_pulse_count
    transaction_active = False
    total_inserted = 0
    id_trx = None
    payment_token = None
    product_price = 0
    last_pulse_received_time = time.time()  
    insufficient_payment_count = 0  
    pending_pulse_count = 0  
    log_transaction("🔄 Transaksi di-reset ke default.")

@app.route('/api/status', methods=['GET'])
def get_bill_acceptor_status():
    global transaction_active
    
    if transaction_active:
        return jsonify({
            "status": "error",
            "message": "Bill acceptor sedang dalam transaksi"
        }), 409 

    return jsonify({
        "status": "success",
        "message": "Bill acceptor siap digunakan"
    }), 200 

def trigger_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time, pending_pulse_count
    
    while True:
        if transaction_active:
            time.sleep(1) 
            continue

        log_transaction("🔍 Mencari payment token terbaru...")
        
        try:
            response = requests.get(TOKEN_API, timeout=1)
            response_data = response.json()

            if response.status_code == 200 and "data" in response_data:
                for token_data in response_data["data"]:
                    created_time = datetime.datetime.strptime(token_data["CreatedAt"], "%Y-%m-%dT%H:%M:%S.%fZ") 
                    created_time = created_time.replace(tzinfo=datetime.timezone.utc) 
                    age_in_minutes = (datetime.datetime.now(datetime.timezone.utc) - created_time).total_seconds() / 60
                    
                    if age_in_minutes <= 3:  
                        payment_token = token_data["PaymentToken"]
                        log_transaction(f"✅ Token ditemukan: {payment_token}, umur: {age_in_minutes:.2f} menit")

                        # Ambil detail invoice berdasarkan paymentToken
                        invoice_response = requests.get(f"{INVOICE_API}{payment_token}", timeout=5)
                        invoice_data = invoice_response.json()

                        if invoice_response.status_code == 200 and "data" in invoice_data:
                            invoice = invoice_data["data"]
                            if not invoice.get("isPaid", False):
                                id_trx = invoice["ID"]
                                product_price = int(invoice["productPrice"])

                                transaction_active = True
                                pending_pulse_count = 0 
                                last_pulse_received_time = time.time()
                                log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")
                                pi.write(EN_PIN, 1)
                                threading.Thread(target=start_timeout_timer, daemon=True).start()
                                return
                            else:
                                log_transaction(f"⚠️ Invoice {payment_token} sudah dibayar, mencari lagi...")

            log_transaction("✅ Tidak ada payment token yang memenuhi syarat. Menunggu...")
            time.sleep(1)

        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ Gagal mengambil daftar payment token: {e}")
            time.sleep(1)

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    threading.Thread(target=trigger_transaction, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
