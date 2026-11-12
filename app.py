import concurrent.futures as cf
import csv
import os
import re
import sqlite3
import time
from collections import Counter
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Info Sepatu", page_icon="👟", layout="centered")

BASE_URL = "https://kenari.id/v1"
MODEL = "deepseek-v4-flash"
SERPER_URL = "https://google.serper.dev/search"
DB_FILE = "sepatu.db"
MAP_FILE = "ean_map.csv"
KODE_RE = re.compile(r"\b[A-Z0-9]{2,14}(?:-[A-Z0-9]{2,6})?\b")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
SISTEM = (
    "Kamu asisten yang merangkum informasi produk sepatu dari beberapa sumber web. "
    "Gunakan HANYA informasi dari teks yang diberikan. Jangan menambah atau mengarang fakta. "
    "Ambil isi dari judul, cuplikan, dan teks halaman, termasuk kalimat promosi produk. "
    "Tulis 'Tidak disebutkan' hanya jika benar-benar tidak ada di semua sumber. "
    "Jawab dalam bahasa Indonesia yang ramah, dengan format persis seperti ini:\n"
    "**Nama Produk:** ...\n\n"
    "**Benefit:**\n- ...\n\n"
    "**Keunggulan:**\n- ...\n\n"
    "**Bahan:**\n- ...\n\n"
    "**Fungsi:**\n- ..."
)

CSS = """
<style>
.stApp { background: #FFFFFF; }
.stApp, .stApp p, .stApp li, .stApp label, .stApp span, .stApp h1, .stApp h2, .stApp h3 {
    color: #1E2A21;
}
.block-container { max-width: 760px; padding-top: 2.2rem; }
.hero { text-align: center; padding: 8px 0 24px; }
.hero h1 { font-size: 40px; margin: 0 0 6px; color: #1F7A5C; }
.hero p { color: #5A6B61; font-size: 17px; margin: 0; }
.stTextInput input {
    border-radius: 16px; border: 2px solid #CFE8D8; padding: 14px 16px; font-size: 18px;
}
.stTextInput input:focus { border-color: #1F7A5C; box-shadow: 0 0 0 3px #E8F6EE; }
.stButton button, div[data-testid="stFormSubmitButton"] button {
    border-radius: 999px; font-weight: 700; background: #1F7A5C; color: #FFFFFF;
    border: none; padding: 12px 20px; font-size: 17px;
}
.stButton button:hover, div[data-testid="stFormSubmitButton"] button:hover {
    background: #16604A; color: #FFFFFF;
}
.badge {
    display: inline-block; background: #E8F6EE; color: #1F7A5C;
    padding: 6px 14px; border-radius: 999px; font-size: 15px; font-weight: 700;
}
.kartu { border-radius: 20px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ean (ean TEXT PRIMARY KEY, kode TEXT, dibuat TEXT)"
    )
    conn.commit()
    return conn


def ambil_sepatu(conn, kode):
    return conn.execute(
        "SELECT info, sumber, dibuat FROM sepatu WHERE kode = ?", (kode,)
    ).fetchone()


def simpan_sepatu(conn, kode, info, sumber):
    conn.execute(
        "INSERT OR REPLACE INTO sepatu VALUES (?, ?, ?, ?)",
        (kode, info, sumber, time.strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()


def ambil_ean_db(conn, ean):
    row = conn.execute("SELECT kode FROM ean WHERE ean = ?", (ean,)).fetchone()
    return row[0] if row else None


def simpan_ean(conn, ean, kode):
    conn.execute(
        "INSERT OR REPLACE INTO ean VALUES (?, ?, ?)",
        (ean, kode, time.strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()


# ---------- EAN ----------
def is_ean(teks):
    s = teks.replace(" ", "").replace("-", "")
    return s.isdigit() and len(s) in (8, 12, 13, 14)


def variasi_ean(ean):
    hasil = {ean}
    if len(ean) == 12:
        hasil.add("0" + ean)
    if len(ean) == 13 and ean.startswith("0"):
        hasil.add(ean[1:])
    return hasil


def muat_peta_csv():
    if not os.path.exists(MAP_FILE):
        return {}
    with open(MAP_FILE, encoding="utf-8-sig") as f:
        return {
            row["ean"].strip(): row["kode"].strip().upper()
            for row in csv.DictReader(f)
            if row.get("ean") and row.get("kode")
        }


def kode_dari_teks(teks, ean):
    """Ambil kandidat kode artikel (huruf dan angka) dari teks hasil pencarian."""
    kandidat = Counter()
    for m in set(KODE_RE.findall(teks.upper())):
        if m == ean or not (re.search(r"[A-Z]", m) and re.search(r"\d", m)):
            continue
        if len(m.replace("-", "")) < 5:
            continue
        kandidat[m] += 1
    return kandidat


def cari_kode_dari_ean(ean, serper_key):
    hasil = cari_web(serper_key, [ean, f"{ean} sepatu", f"barcode {ean} sneakers"])
    if not hasil:
        return None
    # Utamakan hasil yang benar-benar memuat EAN itu
    variasi = {normal(v) for v in variasi_ean(ean)}
    relevan = [h for h in hasil if any(v in normal(h["judul"] + " " + h["cuplikan"]) for v in variasi)]
    sumber = relevan or hasil
    total = Counter()
    for h in sumber:
        total.update(kode_dari_teks(h["judul"] + " " + h["cuplikan"] + " " + h["link"], ean))
    if not total:
        return None
    return total.most_common(1)[0][0]


def tentukan_kode_dari_ean(conn, ean, serper_key):
    for v in variasi_ean(ean):
        peta = muat_peta_csv()
        if v in peta:
            return peta[v], "daftar manual"
    tersimpan = ambil_ean_db(conn, ean)
    if tersimpan:
        return tersimpan, "sudah pernah dicari"
    if not serper_key:
        return None, "kunci Serper belum diatur"
    kode = cari_kode_dari_ean(ean, serper_key)
    if kode:
        simpan_ean(conn, ean, kode)
        return kode, "terdeteksi otomatis dari EAN"
    return None, "tidak ditemukan"


# ---------- PENCARIAN INFO ----------
def normal(teks):
    return re.sub(r"[^a-z0-9]", "", teks.lower())


def cari_web(serper_key, queries):
    hasil, terlihat = [], set()
    for q in queries:
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
    return hasil


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


def cari_info_sepatu(kode, serper_key, kenari_key, status):
    k = normal(kode)
    status.write("🔎 Mencari di berbagai sumber")
    hasil = cari_web(
        serper_key,
        [f"{kode} sepatu", f"{kode} sneakers", f"{kode} shoes", f"{kode} bahan spesifikasi"],
    )
    cocok = [h for h in hasil if k in normal(h["judul"] + " " + h["cuplikan"] + " " + h["link"])]
    if not cocok:
        return None

    cocok = cocok[:8]
    status.write(f"📄 Membaca {len(cocok)} halaman")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        isi = list(ex.map(ambil_teks, [h["link"] for h in cocok]))
    for h, t in zip(cocok, isi):
        h["isi"] = t
    # halaman yang juga memuat kode di isinya diletakkan di depan
    cocok.sort(key=lambda h: k in normal(h["isi"]), reverse=True)

    bahan = "\n\n".join(
        f"[{i}] {h['judul']}\nLink: {h['link']}\nCuplikan: {h['cuplikan']}\n"
        f"Isi halaman: {h['isi'] or '-'}"
        for i, h in enumerate(cocok, 1)
    )
    status.write("🧠 Merapikan dengan AI")
    info = rapikan_ai(kenari_key, kode, bahan)
    return info, [h["link"] for h in cocok]


# ---------- TAMPILAN ----------
def tampil_hasil(kode, info, links, keterangan):
    with st.container(border=True):
        st.markdown(
            f'<span class="badge">👟 {kode}</span> &nbsp; <small>{keterangan}</small>',
            unsafe_allow_html=True,
        )
        st.markdown(info)
        with st.expander("Lihat sumber informasi"):
            for link in links:
                if link:
                    domain = urlparse(link).netloc.replace("www.", "")
                    st.markdown(f"- [{domain}]({link})")


st.markdown(
    '<div class="hero"><h1>👟 Info Sepatu</h1>'
    "<p>Scan barcode EAN di kotak sepatu, atau ketik kode artikelnya.</p></div>",
    unsafe_allow_html=True,
)

conn = buka_db()
tab_cari, tab_riwayat = st.tabs(["🔍 Cari", "📚 Riwayat"])

with tab_cari:
    with st.form("form_cari", clear_on_submit=True):
        masukan = st.text_input(
            "Scan EAN atau ketik kode artikel",
            placeholder="Klik kolom ini, lalu scan",
        )
        ulang = st.checkbox("Cari ulang dari internet (abaikan data tersimpan)")
        kirim = st.form_submit_button("Cari info", use_container_width=True)

    if kirim:
        raw = masukan.strip()
        k_serper = get_secret("SERPER_KEY")
        k_kenari = get_secret("KENARI_KEY")

        if not raw:
            st.warning("Belum ada yang discan. Klik kolom di atas, lalu scan barcode.")
        else:
            kode, asal = None, ""
            if is_ean(raw):
                ean = raw.replace(" ", "").replace("-", "")
                with st.spinner("Menerjemahkan EAN ke kode artikel..."):
                    kode, asal = tentukan_kode_dari_ean(conn, ean, k_serper)
                if not kode:
                    st.error(f"EAN {ean} belum bisa diterjemahkan ({asal}). Ketik kode artikelnya secara manual.")
            else:
                kode = raw.upper()

            if kode:
                data = ambil_sepatu(conn, kode)
                if data and not ulang:
                    tampil_hasil(kode, data[0], data[1].split("\n"), f"dari data tersimpan ({data[2]})")
                elif not (k_serper and k_kenari):
                    st.error("Kunci API belum diatur. Buka Settings > Secrets di Streamlit, lalu isi SERPER_KEY dan KENARI_KEY.")
                else:
                    try:
                        with st.status("Sedang mencari info sepatu...", expanded=True) as status:
                            hasil = cari_info_sepatu(kode, k_serper, k_kenari, status)
                            if hasil:
                                status.update(label="Selesai! ✨", state="complete", expanded=False)
                            else:
                                status.update(label="Belum ada hasil yang cocok", state="error")
                    except Exception as e:
                        hasil = None
                        st.error(f"Maaf, ada kendala saat mencari: {e}")

                    if hasil:
                        info, links = hasil
                        simpan_sepatu(conn, kode, info, "\n".join(links))
                        ket = f"{asal}" if asal else "baru dicari"
                        tampil_hasil(kode, info, links, ket)

with tab_riwayat:
    st.caption("Sepatu yang pernah dicari, dan EAN yang sudah diterjemahkan.")
    df = pd.read_sql_query(
        "SELECT kode AS Kode, dibuat AS Dibuat FROM sepatu ORDER BY dibuat DESC", conn
    )
    st.subheader("Sepatu")
    if df.empty:
        st.info("Belum ada data. Cari sepatu pertama Anda di tab Cari.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)
    st.subheader("EAN")
    df_ean = pd.read_sql_query(
        "SELECT ean AS EAN, kode AS Kode, dibuat AS Dibuat FROM ean ORDER BY dibuat DESC", conn
    )
    if df_ean.empty:
        st.info("Belum ada EAN yang diterjemahkan.")
    else:
        st.dataframe(df_ean, hide_index=True, use_container_width=True)