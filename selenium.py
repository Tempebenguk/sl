from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time
import re

# Konfigurasi untuk menghubungkan ke Chromium yang sudah berjalan
chrome_options = Options()
chrome_options.debugger_address = "127.0.0.1:9222"

# Hubungkan ke Chromium yang sedang berjalan
driver = webdriver.Chrome(options=chrome_options)

# Pola regex untuk mendeteksi UUID dalam URL
uuid_pattern = re.compile(r"ordercashdekstop/([a-f0-9\-]+)")

while True:
    try:
        current_url = driver.current_url
        print("URL Aktif:", current_url)

        # Cek apakah URL sesuai dengan pola UUID
        match = uuid_pattern.search(current_url)
        if match:
            uuid = match.group(1)  # Ambil UUID dari regex
            print("UUID ditemukan:", uuid)

        time.sleep(5)  # Cek URL setiap 5 detik

    except Exception as e:
        print("Error:", e)
        break
