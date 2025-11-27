import json
import csv
import os
from datetime import datetime, date
from collections import Counter
from io import BytesIO

import streamlit as st
from openpyxl import Workbook

# ---------------------------------------------------------
# PUTEVI I BAZA
# ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

os.makedirs(DATA_DIR, exist_ok=True)


def parse_timestamp(ts_str: str) -> datetime:
    """
    Parsiranje time_stamp stringa u datetime.
    Podržava:
    - 2025-11-01T07:31:56+0000
    - 2025-11-01T07:31:56
    - 2025-11-01T07:31:56Z
    """
    ts_str = (ts_str or "").strip()

    # 1) kompletan format s offsetom
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        pass

    # 2) bez vremenske zone
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass

    # 3) ISO "Z" na kraju
    if ts_str.endswith("Z"):
        try:
            return datetime.strptime(ts_str[:-1], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass

    raise ValueError(f"Ne mogu parsirati time_stamp: {ts_str}")


@st.cache_data(show_spinner="Učitavanje podataka iz data/ foldera...")
def load_all_data():
    """
    Učita SVE JSON/CSV datoteke iz data/ foldera.
    Vraća:
    - data: list(dict)
    - org_names: sortirana lista naziva organizacija
    - min_date, max_date: raspon datuma u bazi (date ili None)
    """
    data = []
    org_id_to_name = {}
    min_date = None
    max_date = None

    def update_min_max(d: date):
        nonlocal min_date, max_date
        if d is None:
            return
        if min_date is None or d < min_date:
            min_date = d
        if max_date is None or d > max_date:
            max_date = d

    def load_json(path: str):
        nonlocal data, org_id_to_name
        try:
            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)
        except Exception as e:
            st.warning(f"Ne mogu učitati JSON datoteku {os.path.basename(path)}: {e}")
            return

        if not isinstance(arr, list):
            st.warning(f"JSON datoteka nije lista zapisa: {os.path.basename(path)}")
            return

        for rec in arr:
            ts_str = rec.get("time_stamp")
            if not ts_str:
                continue

            try:
                d = parse_timestamp(ts_str).date()
                update_min_max(d)
            except Exception:
                continue

            oid = rec.get("organization_id")
            oname = rec.get("organization_name")
            if oid and oname and oid not in org_id_to_name:
                org_id_to_name[oid] = oname

            data.append(rec)

    def load_csv(path: str):
        nonlocal data, org_id_to_name
        try:
            with open(path, "r", encoding="cp1250", newline="") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    vin = (row.get("vin") or "").strip()
                    order_date = (row.get("order_date") or "").strip()
                    org_id = (row.get("organisation") or "").strip()
                    user_id = (row.get("order_client") or "").strip()

                    if not vin or not order_date:
                        continue

                    try:
                        dt = datetime.strptime(order_date, "%Y-%m-%d %H:%M:%S")
                        d = dt.date()
                        update_min_max(d)
                        time_stamp = dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
                    except ValueError:
                        continue

                    org_name = org_id_to_name.get(org_id, org_id)

                    rec = {
                        "user_id": user_id,
                        "organization_id": org_id,
                        "organization_name": org_name,
                        "query_vin": vin,
                        "time_stamp": time_stamp,
                        "response_type": None,
                    }
                    data.append(rec)
        except Exception as e:
            st.warning(f"Ne mogu učitati CSV datoteku {os.path.basename(path)}: {e}")

    # Prođi kroz sve datoteke u data/
    for fname in sorted(os.listdir(DATA_DIR)):
        path = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(fname)[1].lower()
        if ext == ".json":
            load_json(path)
        elif ext == ".csv":
            load_csv(path)

    # lista organizacija
    org_names = sorted(
        {
            i.get("organization_name")
            for i in data
            if i.get("organization_name")
        }
    )

    return data, org_names, min_date, max_date


def calculate_stats(data, org_name, d_from: date, d_to: date):
    """
    Glavna logika filtriranja:
    - filtrira po organizaciji (ako je odabrana)
    - filtrira po rasponu datuma
    - uklanja duplikate po (query_vin, time_stamp)
    Vraća:
    - export_rows: list dict-ova spremnih za Excel
    - per_day: Counter po danu (za grafove kasnije)
    - top_vins: lista (vin, count) – top 5
    """
    unique_records = {}
    per_day = Counter()
    vin_counter = Counter()

    for item in data:
        # filter organizacije
        if org_name and item.get("organization_name") != org_name:
            continue

        ts_str = item.get("time_stamp")
        if not ts_str:
            continue

        try:
            ts = parse_timestamp(ts_str)
        except ValueError:
            continue

        d = ts.date()
        if not (d_from <= d <= d_to):
            continue

        qvin = item.get("query_vin")
        key = (qvin, ts_str)

        if key not in unique_records:
            row = {
                "user_id": item.get("user_id"),
                "organization_id": item.get("organization_id"),
                "organization_name": item.get("organization_name"),
                "query_vin": item.get("query_vin"),
                "time_stamp": item.get("time_stamp"),
            }
            unique_records[key] = row

            # statistika po danu
            per_day[d] += 1

            # statistika po VIN-u
            if qvin:
                vin_counter[qvin] += 1

    export_rows = list(unique_records.values())
    top_vins = vin_counter.most_common(5)

    return export_rows, per_day, top_vins


def make_excel_bytes(rows):
    """
    Kreira Excel (u memoriji) iz danih redaka i vraća bytes za download.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Upiti"

    headers = ["user_id", "organization_id", "organization_name", "query_vin", "time_stamp"]
    ws.append(headers)

    for r in rows:
        ws.append(
            [
                r.get("user_id"),
                r.get("organization_id"),
                r.get("organization_name"),
                r.get("query_vin"),
                r.get("time_stamp"),
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------

st.set_page_config(page_title="MEVA - AH Statistika", layout="wide")

st.title("MEVA - AH Statistika (web)")
st.write("Web verzija alata za pregled broja upita po organizaciji i datumu.")

# Učitavanje podataka
data, org_names, min_date, max_date = load_all_data()

if not data:
    st.warning(
        "Nema podataka u `data/` folderu.\n\n"
        "Dodaj JSON/CSV datoteke (isti format kao u desktop aplikaciji) "
        "i redeployaj aplikaciju."
    )
    st.stop()

# ---------------------------------------------------------
# FILTRI
# ---------------------------------------------------------
st.markdown("### Kriteriji pretrage")

col1, col2, col3 = st.columns([2, 1, 1])

with col1:
    org_options = ["(Sve organizacije)"] + org_names
    selected_org = st.selectbox("🏢 Naziv organizacije", org_options)

with col2:
    default_from = min_date or date(2020, 1, 1)
    d_from = st.date_input("📅 Datum OD", value=default_from)

with col3:
    default_to = max_date or date.today()
    d_to = st.date_input("📅 Datum DO", value=default_to)

if d_from > d_to:
    st.error("❌ Datum OD ne može biti veći od datuma DO.")
    st.stop()

# ---------------------------------------------------------
# GUMB ZA IZRAČUN
# ---------------------------------------------------------
if st.button("🔍 Prikaži rezultat"):
    org_filter = selected_org if selected_org != "(Sve organizacije)" else ""

    export_rows, per_day, top_vins = calculate_stats(data, org_filter, d_from, d_to)

    st.markdown("### Rezultat")

    st.metric("📊 Broj upita", len(export_rows))

    if not export_rows:
        st.info("Nema zapisa za zadane kriterije.")
    else:
        # tablica (prvih 200 redova radi preglednosti)
        st.write("Prvih 200 zapisa:")
        st.dataframe(export_rows[:200], use_container_width=True)

        # priprema Excel datoteke za download
        excel_bytes = make_excel_bytes(export_rows)

        file_name_org = (
            org_filter.replace(" d.d.", "")
            .replace(" d.d", "")
            .replace(" ", "_")
            .replace(".", "")
        )
        if file_name_org:
            file_name = f"AH_{file_name_org}.xlsx"
        else:
            file_name = "AH_SVE_ORGANIZACIJE.xlsx"

        st.download_button(
            label="📥 Preuzmi Excel",
            data=excel_bytes,
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.success("Export je spreman za preuzimanje.")

else:
    st.info("Odaberi kriterije i klikni **'Prikaži rezultat'**.")
