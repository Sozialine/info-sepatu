import csv
import os
import re
import sqlite3
import time

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Kicks Information", page_icon="👟", layout="centered")

BASE_URL = "https://kenari.id/v1"
MODEL = "deepseek-v4-flash"
SERPER_URL = "https://google.serper.dev/search"
DB_FILE = "sepatu.db"
MAP_FILE = "ean_map.csv"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
SISTEM = (
    "Kamu asisten yang merangkum informasi produk sepatu dari hasil pencarian web. "
    "Gunakan HANYA informasi dari teks yang diberikan. Jangan menambah atau mengarang fakta. "
    "Ambil isi dari judul, cuplikan, dan teks halaman, termasuk kalimat promosi produk. "
    "Tulis 'Tidak disebutkan' hanya jika benar-benar tidak ada sama sekali. "
    "Jawab dalam bahasa Indonesia dengan format persis seperti ini:\n"
    "**Nama Produk:** ...\n\n"
    "**Benefit:**\n- ...\n\n"
    "**Keunggulan:**\n- ...\n\n"
    "**Bahan:**\n- ...\n\n"
    "**Fungsi:**\n- ..."
)


# ---------- KUNCI RAHASIA ----------
def get_secret(nama):
    try:
        return st.secrets[nama]
    except Exception:
        return None


# ---------- DATABASE ----------
def buka_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sepatu (kode TEXT PRIMARY KEY, info TEXT, "
        "sumber TEXT, dibuat TEXT)"
    )
    return conn


def ambil_db(conn, kode):
    return conn.execute(
        "SELECT info, sumber, dibuat FROM sepatu WHERE kode = ?", (kode,)
    ).fetchone()


def simpan_db(conn, kode, info, sumber):
    conn.execute(
        "INSERT OR REPLACE INTO sepatu VALUES (?, ?, ?, ?)",
        (kode, info, sumber, time.strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()


def daftar_db(conn):
    return pd.read_sql_query(
        "SELECT kode AS Kode, dibuat AS Dibuat FROM sepatu ORDER BY dibuat DESC", conn
    )


# ---------- DAFTAR EAN ----------
def muat_peta_ean():
    if not os.path.exists(MAP_FILE):
        return {}
    with open(MAP_FILE, encoding="utf-8") as f:
        return {
            row["ean"].strip(): row["kode"].strip().upper()
            for row in csv.DictReader(f)
            if row.get("ean") and row.get("kode")
        }


# ---------- PENCARIAN ----------
def normal(teks):
    return re.sub(r"[^a-z0-9]", "", teks.lower())


def cari_web(serper_key, kode):
    hasil, terlihat = [], set()
    for q in [f"{kode} sepatu", f"{kode} sepatu bahan spesifikasi fungsi"]:
        r = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": q, "gl": "id", "hl": "id", "num": 10},
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("organic", []):
            link = item.get("link", "")
            if link and link not in terlihat:
                terlihat.add(link)
                hasil.append(
                    {
                        "judul": item.get("title", ""),
                        "link": link,
                        "cuplikan": item.get("snippet", ""),
                    }
                )
    k = normal(kode)
    cocok = [h for h in hasil if k in normal(h["judul"] + " " + h["cuplikan"] + " " + h["link"])]
    return cocok[:6]


def ambil_teks(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            t.decompose()
        return " ".join(soup.get_text(" ").split())[:4000]
    except Exception:
        return ""


def rapikan_ai(kenari_key, kode, bahan):
    r = requests.post(
        BASE_URL.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + kenari_key, "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SISTEM},
                {"role": "user", "content": "Kode artikel: " + kode + "\n\n" + bahan},
            ],
        },
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def cari_baru(kode, k_serper, k_kenari):
    with st.status("Sedang mencari info sepatu...", expanded=True) as status:
        st.write("🔎 Mencari di internet")
        hasil = cari_web(k_serper, kode)
        if not hasil:
            status.update(label="Belum ada hasil yang cocok", state="error")
            return None

        st.write(f"📄 Membaca {len(hasil)} halaman")
        bagian = []
        for i, h in enumerate(hasil, 1):
            isi = ambil_teks(h["link"])
            bagian.append(
                f"[{i}] {h['judul']}\nLink: {h['link']}\nCuplikan: {h['cuplikan']}\n"
                f"Isi halaman: {isi or '-'}"
            )

        st.write("🧠 Merapikan dengan AI")
        info = rapikan_ai(k_kenari, kode, "\n\n".join(bagian))
        status.update(label="Selesai! ✨", state="complete", expanded=False)

    sumber = "\n".join(h["link"] for h in hasil)
    return info, sumber


def tampil_hasil(kode, info, sumber, keterangan):
    with st.container(border=True):
        st.success(f"👟 {kode}  ·  {keterangan}")
        st.markdown(info)
        with st.expander("Lihat sumber"):
            for link in sumber.split("\n"):
                if link:
                    st.write(link)


# ---------- TAMPILAN ----------
st.title("👟 Info Sepatu")
st.write("Scan barcode EAN di kotak sepatu, atau ketik kode artikelnya. Infonya langsung muncul.")

conn = buka_db()
peta = muat_peta_ean()

tab_cari, tab_riwayat = st.tabs(["🔍 Cari", "📚 Riwayat"])

with tab_cari:
    with st.form("form_cari", clear_on_submit=True):
        masukan = st.text_input(
            "Scan EAN atau ketik kode artikel",
            placeholder="Klik kolom ini, lalu scan",
        )
        ulang = st.checkbox("Cari ulang dari internet (abaikan data tersimpan)")
        kirim = st.form_submit_button("Cari info", type="primary", use_container_width=True)

    if kirim:
        teks = masukan.strip()
        k_serper = get_secret("SERPER_KEY")
        k_kenari = get_secret("KENARI_KEY")

        if not teks:
            st.warning("Belum ada yang discan. Klik kolom di atas, lalu scan barcode.")
        else:
            kode = None
            if teks.isdigit() and len(teks) in (8, 12, 13, 14):
                kode = peta.get(teks)
                if not kode:
                    st.error(
                        f"EAN {teks} belum ada di daftar. Ketik kode artikelnya secara manual, "
                        "lalu tambahkan EAN ini ke ean_map.csv."
                    )
            else:
                kode = teks.upper()

            if kode:
                data = ambil_db(conn, kode)
                if data and not ulang:
                    tampil_hasil(kode, data[0], data[1], f"dari data tersimpan ({data[2]})")
                elif not (k_serper and k_kenari):
                    st.error(
                        "Kunci API belum diatur. Buka menu Settings > Secrets di Streamlit, "
                        "lalu isi SERPER_KEY dan KENARI_KEY."
                    )
                else:
                    try:
                        hasil = cari_baru(kode, k_serper, k_kenari)
                    except Exception as e:
                        hasil = None
                        st.error(f"Maaf, ada kendala saat mencari: {e}")
                    if hasil:
                        info, sumber = hasil
                        simpan_db(conn, kode, info, sumber)
                        tampil_hasil(kode, info, sumber, "baru dicari")
                    else:
                        st.info("Belum ketemu di internet. Coba periksa kode artikelnya.")

with tab_riwayat:
    st.caption("Semua sepatu yang pernah dicari.")
    df = daftar_db(conn)
    if df.empty:
        st.info("Belum ada data. Cari sepatu pertama Anda di tab Cari.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)
