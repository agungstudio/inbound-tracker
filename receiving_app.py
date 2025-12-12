import streamlit as st
import pandas as pd
from supabase import create_client
from datetime import datetime, timezone
import time
import io
import json
import logging
from postgrest.exceptions import APIError
from openpyxl.styles import PatternFill, Font, Alignment

# --- KONFIGURASI [v1.23 - Inbound Status Control] ---
SUPABASE_URL = st.secrets.get("SUPABASE_URL")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY")
DAFTAR_CHECKER = ["Agung", "Al Fath", "Reza", "Rico", "Sasa", "Mita", "Koordinator"]
RESET_PIN = "123456" 
SESSION_KEY_CHECKER = "current_checker_name_receiving" 
RECEIVING_TABLE = "receiving_validation" # Nama Tabel di Supabase

# Configure basic logging
logging.basicConfig(level=logging.INFO)

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("⚠️ KONFIGURASI DATABASE BELUM ADA.")
    st.markdown("Harap masukkan `SUPABASE_URL` dan `SUPABASE_KEY` di **Secrets Streamlit Cloud** atau file `.streamlit/secrets.toml`.")
    st.markdown("**(Pastikan `SUPABASE_KEY` adalah SERVICE ROLE KEY/MASTER KEY untuk bypass RLS)**")
    st.stop()

# Memaksa Streamlit me-rehash koneksi dengan Supabase
@st.cache_resource(hash_funcs={type(st.secrets): lambda x: (x.get("SUPABASE_URL"), x.get("SUPABASE_KEY"))})
def init_connection():
    try:
        logging.info("Attempting to connect to Supabase using Master Key method...")
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.table(RECEIVING_TABLE).select("id").limit(0).execute()
        return client
    except Exception as e:
        logging.error(f"Failed to connect to Supabase: {e}")
        st.error("❌ KONEKSI DATABASE GAGAL. Pastikan URL dan Kunci Supabase Anda (Service Role Key) benar.")
        st.stop()

supabase = init_connection()

# --- FUNGSI HELPER WAKTU & KONVERSI ---
def parse_supabase_timestamp(timestamp_str):
    """Mengubah string timestamp Supabase menjadi objek datetime yang aman"""
    try:
        if timestamp_str and timestamp_str.endswith('Z'):
             timestamp_str = timestamp_str[:-1] + '+00:00'
        return datetime.fromisoformat(timestamp_str) if timestamp_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

def convert_df_to_excel(df, sheet_name='Data_Receiving'):
    """Mengubah DataFrame menjadi file Excel dengan Header Cantik"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        # FIX V1.23: Tambah kolom is_inbound
        cols = ['gr_number', 'sku', 'nama_barang', 'qty_po', 'qty_fisik', 'qty_diff', 'keterangan', 'jenis', 'is_inbound', 'sn_list', 'updated_by', 'updated_at']
        
        df_export = df.copy()
        if 'sn_list' in df_export.columns:
             df_export['sn_list'] = df_export['sn_list'].apply(lambda x: "; ".join(x) if isinstance(x, list) else (x if pd.notna(x) else ''))
        
        df_export['qty_diff'] = df_export['qty_fisik'] - df_export['qty_po']
        
        available_cols = [c for c in cols if c in df_export.columns]
        df_export = df_export[available_cols] if not df_export.empty else df_export
        
        df_export.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        
        blibli_blue_fill = PatternFill(start_color="0095DA", end_color="0095DA", fill_type="solid")
        white_bold_font = Font(color="FFFFFF", bold=True, size=11)
        center_align = Alignment(horizontal='center', vertical='center')
        
        for cell in worksheet[1]:
            cell.fill = blibli_blue_fill
            cell.font = white_bold_font
            cell.alignment = center_align
            
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) if cell.value else "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 5
            
    return output.getvalue()

# --- FUNGSI HELPER DATABASE ---

def get_active_session_info():
    """Mengambil SEMUA GR number sesi aktif saat ini"""
    try:
        # Mengambil semua GR number yang aktif
        res = supabase.table(RECEIVING_TABLE).select("gr_number").eq("is_active", True).execute()
        active_grs = sorted(list(set([x['gr_number'] for x in res.data])))
        return active_grs if active_grs else ["Belum Ada Sesi Aktif"]
    except Exception as e:
        logging.warning(f"Failed to get active session info: {e}")
        return ["- Error Koneksi -"]

def get_data(gr_number=None, search_term=None, only_active=True):
    """Mengambil data GR untuk dicek, berdasarkan GR number yang dipilih"""
    query = supabase.table(RECEIVING_TABLE).select("*")
    
    if gr_number:
        query = query.eq("gr_number", gr_number)
    
    if only_active: 
        query = query.eq("is_active", True)

    # FIX V1.23: Handle missing is_inbound column if SQL hasn't been run
    select_fields = "*"
    if 'is_inbound' not in supabase.table(RECEIVING_TABLE).select("is_inbound").limit(0).execute().data:
        select_fields = "*, false as is_inbound"
    query = supabase.table(RECEIVING_TABLE).select(select_fields)
    
    start_time = datetime.now(timezone.utc)
    try:
        response = query.order("nama_barang").execute()
    except Exception as e:
        st.error(f"Gagal mengambil data dari Supabase. Cek RLS: {e}")
        return pd.DataFrame()
        
    df = pd.DataFrame(response.data)

    # Pastikan is_inbound ada di DF (default false jika tidak ada di DB)
    if 'is_inbound' not in df.columns: df['is_inbound'] = False
    if 'keterangan' not in df.columns: df['keterangan'] = ""
    if 'jenis' not in df.columns: df['jenis'] = "Stok"

    # Deserialisasi sn_list
    if 'sn_list' in df.columns:
        df['sn_list'] = df['sn_list'].apply(lambda x: json.loads(x) if isinstance(x, str) and x.startswith('[') else (x if isinstance(x, list) else []))

    if not df.empty and search_term:
        df = df[df['nama_barang'].str.contains(search_term, case=False, na=False) | 
                df['sku'].str.contains(search_term, case=False, na=False)]
    
    st.session_state['data_loaded_time'] = start_time
    st.session_state['current_df'] = df.copy()
    
    return df

def get_db_updated_at(id_barang):
    """Mengambil updated_at dari DB saat ini untuk cek konflik"""
    try:
        res = supabase.table(RECEIVING_TABLE).select("updated_at, updated_by").eq("id", id_barang).limit(1).execute()
        if res.data and len(res.data) > 0:
            data = res.data[0]
            return data.get('updated_at'), data.get('updated_by')
        else:
            return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(), "SYSTEM"
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(), "SYSTEM_ERROR"

# --- FUNGSI ADMIN: PROSES DATA ---

def process_and_insert(df, gr_number):
    """Memproses DF Master GR dan menginput ke DB"""
    
    required_cols = ['SKU', 'Nama Barang', 'Qty PO', 'Tipe Barang']
    if not all(col in df.columns for col in required_cols):
        return False, f"File Excel harus memiliki kolom: {', '.join(required_cols)}"
        
    # === [FIX v1.4: Robust NaN Handling] ===
    if 'Qty PO' in df.columns:
        df['Qty PO'] = df['Qty PO'].fillna(0).astype(int)
    
    df['Tujuan (Stok/Display)'] = df['Tujuan (Stok/Display)'].fillna('')
    df['Keterangan Awal'] = df['Keterangan Awal'].fillna('')
    df['SKU'] = df['SKU'].fillna('')
    df['Nama Barang'] = df['Nama Barang'].fillna('')
    df['Tipe Barang'] = df['Tipe Barang'].fillna('NON-SN')

    data_to_insert = []
    
    for _, row in df.iterrows():
        
        jenis_val = str(row.get('Tujuan (Stok/Display)')).strip()
        if jenis_val == '': jenis_val = 'Stok'
        keterangan_val = str(row.get('Keterangan Awal')).strip()
        keterangan_val = keterangan_val if keterangan_val else None
        tipe_barang = str(row.get('Tipe Barang')).upper()
        is_sn_item = tipe_barang == 'SN'

        item = {
            "sku": str(row.get('SKU')).strip(),
            "nama_barang": str(row.get('Nama Barang')).strip(),
            "kategori_barang": tipe_barang,
            "qty_po": int(row.get('Qty PO', 0)),
            "qty_fisik": 0, "updated_by": "-", "is_active": True, "gr_number": gr_number,
            "jenis": jenis_val,
            "keterangan": keterangan_val, 
            "sn_list": [] if is_sn_item else None,
            "is_inbound": False # FIX V1.23: Semua item baru status Inbound = FALSE
        }
        data_to_insert.append(item)
    
    if not data_to_insert:
        return False, "Tidak ada data valid untuk diinput."
        
    try:
        batch_size = 500
        for i in range(0, len(data_to_insert), batch_size):
            supabase.table(RECEIVING_TABLE).insert(data_to_insert[i:i+batch_size]).execute()
            
        return True, len(data_to_insert)
    except APIError as e:
         return False, f"Gagal API Supabase: {e.message}. Pastikan kolom DB sudah dibuat dengan benar."
    except Exception as e:
         return False, f"Error saat insert data: {str(e)}"

def delete_active_session():
    """Hapus sesi aktif tanpa arsip"""
    try:
        supabase.table(RECEIVING_TABLE).delete().eq("is_active", True).execute()
        return True, "Sesi aktif berhasil dihapus total."
    except Exception as e: return False, str(e)

def delete_blind_receive_item(item_id):
    """FIX V1.22: Hapus item Blind Receive berdasarkan ID"""
    try:
        supabase.table(RECEIVING_TABLE).delete().eq("id", item_id).execute()
        return True, "Item Blind Receive berhasil dihapus."
    except Exception as e:
        error_msg = f"API Error: {str(e)}"
        st.error(f"❌ Gagal menghapus item. DETAIL: {error_msg}")
        return False, error_msg
    
def get_master_template_excel_receiving():
    """Template untuk upload Master GR/PO"""
    data = {
        'SKU': ['SAM-S24-ULT', 'VIV-CBL-01', 'LOG-MOU-05'],
        'Nama Barang': ['Samsung Galaxy S24 Ultra 256GB', 'Vivan Kabel C to C', 'Logitech G502 Hero Mouse'],
        'Qty PO': [10, 500, 25],
        'Tipe Barang': ['SN', 'NON-SN', 'NON-SN'],
        'Tujuan (Stok/Display)': ['Display', 'Stok', 'Stok'],
        'Keterangan Awal': ['Untuk Floor Display', None, None]
    }
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Template_Master_GR')
        worksheet = writer.sheets['Template_Master_GR']
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) if cell.value else "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 5
    return output.getvalue()


# --- LOGIKA CARD VIEW & UPDATE ---

def handle_update_non_sn(row, new_qty, new_jenis, nama_user, loaded_time, keterangan=""):
    """Update QTY dan Jenis untuk barang NON-SN"""
    id_barang = row['id']
    
    original_row_match = st.session_state['current_df'].loc[st.session_state['current_df']['id'] == id_barang]
    if original_row_match.empty: return 0, True
    original_row = original_row_match.iloc[0]
    
    original_qty = original_row['qty_fisik']
    original_jenis = original_row['jenis']
    original_notes = original_row.get('keterangan', '') if original_row.get('keterangan') is not None else ''
    
    keterangan_to_save = keterangan if keterangan.strip() else None

    is_qty_changed = (original_qty != new_qty)
    is_jenis_changed = (original_jenis != new_jenis)
    is_notes_changed = (original_notes.strip() != (keterangan_to_save.strip() if keterangan_to_save else ''))
    
    if is_qty_changed or is_jenis_changed or is_notes_changed:
        
        # Cek Konflik
        db_updated_at_str, updated_by_db = get_db_updated_at(id_barang)
        db_updated_at = parse_supabase_timestamp(db_updated_at_str)
        if db_updated_at > loaded_time:
            st.error(f"⚠️ KONFLIK DATA: **{row['nama_barang']}**! Diubah oleh **{updated_by_db}** pada {db_updated_at.astimezone(None).strftime('%H:%M:%S')}. Muat Ulang!")
            return 0, True

        # Lakukan Update
        update_payload = {
            "qty_fisik": new_qty, 
            "jenis": new_jenis,
            "updated_at": datetime.utcnow().isoformat(), 
            "updated_by": nama_user,
            "keterangan": keterangan_to_save
            # is_inbound tidak diupdate oleh Checker
        }

        try:
            supabase.table(RECEIVING_TABLE).update(update_payload).eq("id", id_barang).execute()
            return 1, False # Success
        except APIError as api_e:
            error_msg = f"API Error: {api_e.message}. Status Code: {api_e.code}" if hasattr(api_e, 'message') else str(api_e)
            st.error(f"❌ Gagal Simpan Item {row['nama_barang']}. DETAIL: {error_msg}")
            # Auto-Heal untuk mengatasi cache RLS
            st.cache_resource.clear()
            st.rerun()
            return 0, True 
        
    return 0, False # No change

def handle_update_sn_list(row, new_sn_list, new_jenis, nama_user, loaded_time, keterangan=""):
    """Update SN List dan Jenis untuk barang SN"""
    id_barang = row['id']
    
    original_row_match = st.session_state['current_df'].loc[st.session_state['current_df']['id'] == id_barang]
    if original_row_match.empty: return 0, True
    original_row = original_row_match.iloc[0]
    
    original_sn_list = original_row.get('sn_list', [])
    original_jenis = original_row['jenis']
    original_notes = original_row.get('keterangan', '') if original_row.get('keterangan') is not None else ''
    
    keterangan_to_save = keterangan if keterangan.strip() else None

    is_sn_list_changed = set(map(str.strip, original_sn_list)) != set(map(str.strip, new_sn_list))
    is_jenis_changed = (original_jenis != new_jenis)
    is_notes_changed = (original_notes.strip() != (keterangan_to_save.strip() if keterangan_to_save else ''))
    
    if is_sn_list_changed or is_jenis_changed or is_notes_changed:
        
        # Cek Konflik
        db_updated_at_str, updated_by_db = get_db_updated_at(id_barang)
        db_updated_at = parse_supabase_timestamp(db_updated_at_str)
        if db_updated_at > loaded_time:
            st.error(f"⚠️ KONFLIK DATA: **{row['nama_barang']}**! Diubah oleh **{updated_by_db}** pada {db_updated_at.astimezone(None).strftime('%H:%M:%S')}. Muat Ulang!")
            return 0, True

        # Lakukan Update
        update_payload = {
            "sn_list": new_sn_list,
            "qty_fisik": len(new_sn_list), # Qty Fisik = Jumlah SN yang dimasukkan
            "jenis": new_jenis,
            "updated_at": datetime.utcnow().isoformat(), 
            "updated_by": nama_user,
            "keterangan": keterangan_to_save
            # is_inbound tidak diupdate oleh Checker
        }

        try:
            # Gunakan json.dumps untuk memastikan array tersimpan dengan benar di Supabase
            payload_to_db = update_payload.copy()
            payload_to_db['sn_list'] = json.dumps(new_sn_list) 
            
            supabase.table(RECEIVING_TABLE).update(payload_to_db).eq("id", id_barang).execute()
            return 1, False # Success
        except APIError as api_e:
            # FIX v1.7: Tampilkan pesan API error spesifik dari Supabase
            error_msg = f"API Error: {api_e.message}. Status Code: {api_e.code}" if hasattr(api_e, 'message') else str(api_e)
            st.error(f"❌ Gagal Simpan Item SN {row['nama_barang']}. DETAIL RLS: {error_msg}")
            # Auto-Heal untuk mengatasi cache RLS
            st.cache_resource.clear()
            st.rerun()
            return 0, True 
        
    return 0, False # No change

def handle_blind_insert(brand, sku, qty, sn_list, tipe_barang, jenis, keterangan, nama_user):
    """Menangani INSERT barang tanpa dokumen (Blind Receive)"""
    
    if not brand or not sku or not keterangan.strip():
        return False, "Brand, SKU, dan Keterangan wajib diisi."
        
    if tipe_barang == 'SN':
        if not sn_list: return False, "Untuk barang SN, Serial Number wajib diisi."
        final_qty = len(sn_list)
        final_sn_list = sn_list
    else:
        if qty <= 0: return False, "Quantity Fisik harus lebih dari 0."
        final_qty = qty
        final_sn_list = None # DB expects None for NON-SN sn_list

    try:
        # Payload mapping user input to DB columns
        payload = {
            "sku": sku.strip(),          # User's input for SKU goes to DB SKU
            "nama_barang": brand.strip(), # User's input for Brand goes to DB Nama Barang
            "kategori_barang": tipe_barang, 
            "qty_po": 0, 
            "qty_fisik": final_qty,
            "jenis": jenis,
            "keterangan": f"BLIND RECEIVE ({nama_user}): {keterangan}",
            "updated_by": nama_user,
            "is_active": True,
            "gr_number": "BLIND-RECEIVE",
            "sn_list": json.dumps(final_sn_list) if final_sn_list is not None else None,
            "is_inbound": False # FIX V1.23: Item Blind Receive juga perlu ditandai Inbound
        }
        
        supabase.table(RECEIVING_TABLE).insert(payload).execute()
        return True, "Barang tanpa dokumen berhasil diregistrasi!"

    except APIError as api_e:
        error_msg = f"API Error: {api_e.message}. Status Code: {api_e.code}" if hasattr(api_e, 'message') else str(api_e)
        st.error(f"❌ Gagal Registrasi Blind Receive. DETAIL: {error_msg}")
        st.cache_resource.clear()
        st.rerun()
        return False, "Terjadi kesalahan database (RLS/API)."
    except Exception as e:
        return False, f"Error umum: {str(e)}"

# --- FUNGSI HALAMAN ADMIN ---

def update_inbound_status(item_id, current_gr, nama_user):
    """FIX V1.23: Update status is_inbound menjadi True"""
    try:
        update_payload = {
            "is_inbound": True,
            "updated_by": f"INBOUND-{nama_user}",
            "updated_at": datetime.utcnow().isoformat(),
            "keterangan": f"INBOUND OK oleh {nama_user}."
        }
        
        supabase.table(RECEIVING_TABLE).update(update_payload).eq("id", item_id).execute()
        return True, f"Item {item_id} berhasil ditandai INBOUND."
    except Exception as e:
        return False, f"Gagal update status inbound: {str(e)}"


# --- HALAMAN CHECKER ---
def page_checker():
    # FIX V1.22: Injeksi CSS untuk membuat input teks lebar penuh di mobile
    st.markdown("""
        <style>
        /* Memaksa elemen input lebar penuh di layar kecil, terutama di tab Scanner */
        textarea, input[type="text"], input[type="number"] {
            width: 100% !important;
            min-width: unset !important;
        }
        /* Memperbaiki tampilan form di layar kecil */
        .stForm {
            width: 100%;
        }
        /* Memperbaiki tampilan selectbox di header SN Scanner */
        [data-testid="stForm"] [data-testid="stSelectbox"] {
            min-width: 100%;
        }
        </style>
        """, unsafe_allow_html=True)

    # FIX V1.19: Mengambil SEMUA sesi aktif
    active_grs = get_active_session_info()
    
    st.title("📱 Validasi Kedatangan Barang")
    
    if SESSION_KEY_CHECKER not in st.session_state:
        st.session_state[SESSION_KEY_CHECKER] = "-- Pilih Petugas --"
    
    opsi_checker = ["-- Pilih Petugas --"] + DAFTAR_CHECKER
    try:
        default_index = opsi_checker.index(st.session_state[SESSION_KEY_CHECKER])
    except ValueError:
        default_index = 0 

    # --- Pilihan Checker dan Sesi GR ---
    with st.container():
        c_pemeriksa, c_gr_session = st.columns([1, 2])
        
        # 1. Nama Checker
        with c_pemeriksa:
            nama_user = st.selectbox("👤 Nama Checker", opsi_checker, index=default_index, key="checker_select")
            if nama_user != st.session_state[SESSION_KEY_CHECKER]:
                 st.session_state[SESSION_KEY_CHECKER] = nama_user
                 st.rerun() 
        
        # 2. Sesi GR/PO Aktif
        current_active_grs = [gr for gr in active_grs if gr != "BLIND-RECEIVE"]
        gr_options = ["-- Pilih Sesi GR/PO --"] + current_active_grs
        
        if 'selected_gr_session' not in st.session_state:
            st.session_state['selected_gr_session'] = gr_options[0]

        with c_gr_session:
            selected_gr = st.selectbox(
                f"📅 Sesi GR/PO Aktif ({len(current_active_grs)} Dokumen)",
                options=gr_options,
                key='gr_session_selector',
                index=0
            )

        st.divider()
    
    final_nama_user = st.session_state[SESSION_KEY_CHECKER]
    
    # -------------------------------------------------------------------------
    # VALIDASI AWAL DAN MUAT DATA
    # -------------------------------------------------------------------------
    if "Pilih Petugas" in final_nama_user:
        st.info("👋 Mohon **pilih nama Anda** terlebih dahulu untuk memulai validasi.")
        st.stop()
        
    if selected_gr == "-- Pilih Sesi GR/PO --":
        st.info("🔎 Mohon **pilih dokumen GR/PO** yang akan Anda validasi.")
        
        # Tampilkan status Blind Receive secara cepat jika ada
        blind_df = get_data(gr_number="BLIND-RECEIVE", only_active=True)
        if not blind_df.empty:
             st.caption(f"ℹ️ Ada {len(blind_df)} item Blind Receive aktif yang menunggu review Admin.")
             
        st.stop()

    # Data hanya dimuat berdasarkan GR yang dipilih
    search_txt = st.text_input(f"🔍 Cari Barang di {selected_gr}", placeholder="Ketik SKU/Nama...")
    
    if st.button("🔄 Muat Ulang Data", key="reload_btn"):
        st.cache_data.clear()
        st.session_state.pop('current_df', None)
        st.rerun()

    df = get_data(gr_number=selected_gr, search_term=search_txt, only_active=True)
    loaded_time = st.session_state.get('data_loaded_time', datetime(1970, 1, 1, tzinfo=timezone.utc))
    
    if df.empty:
        st.info(f"Tidak ada data barang yang valid untuk GR **{selected_gr}**.")
        
    df_sn = df[df['kategori_barang'] == 'SN'].copy()
    df_non = df[df['kategori_barang'] == 'NON-SN'].copy()
    
    total_qty_po = df['qty_po'].sum()
    total_qty_fisik_tercatat = df['qty_fisik'].sum()
    progress_percent = total_qty_fisik_tercatat / total_qty_po if total_qty_po > 0 else 0
    
    st.markdown("---")
    col_metric, col_bar = st.columns([1, 3])
    
    with col_metric:
        st.metric(f"Unit Divalidasi di {selected_gr}", f"{total_qty_fisik_tercatat} / {total_qty_po} (Dari PO)")
    with col_bar:
        st.write("")
        st.caption(f"Progress Dokumen: {progress_percent * 100:.1f}%")
        st.progress(progress_percent)
    st.markdown("---")
    
    # =========================================================================
    # TAB NAVIGATION
    # =========================================================================
    tab_sn, tab_non_sn, tab_adhoc, tab_status = st.tabs([
        "⚡ Pindai SN Cepat", 
        "📦 Input Qty Non-SN", 
        "👻 Tambah Ad Hoc", 
        "📋 Status & Review"
    ])
    
    # -------------------------------------------------------------------------
    # TAB 1: Pindai SN Cepat (Global SN Scanner)
    # -------------------------------------------------------------------------
    with tab_sn:
        if not df_sn.empty:
            st.subheader("⚡ Pemindaian Global Serial Number (Scan Cepat)")
            
            # Pilihan untuk Selectbox: SKU - Nama Barang (ID)
            sn_select_options = ["-- Pilih Barang SN yang Sedang Anda Scan --"] + [
                f"{row['sku']} - {row['nama_barang']} (PO: {row['qty_po']} | Tercatat: {len(row['sn_list'])}) (ID: {row['id'][:4]}...)" 
                for _, row in df_sn.iterrows()
            ]
            
            with st.form("global_sn_form", clear_on_submit=True):
                
                col_sku, col_jenis = st.columns([2, 1])
                
                selected_item_str = col_sku.selectbox(
                    "Pilih Barang SN yang Sedang Anda Scan", 
                    options=sn_select_options,
                    key="global_sn_selector_tab1" # Updated Key
                )

                # Cari ID barang yang dipilih
                selected_row = None
                if "ID:" in selected_item_str:
                    item_id_part = selected_item_str.split('(ID: ')[1].strip(')')
                    selected_id_prefix = item_id_part.split('...')[0]
                    selected_row_match = df_sn[df_sn['id'].str.startswith(selected_id_prefix)]
                    if not selected_row_match.empty:
                        selected_row = selected_row_match.iloc[0].to_dict()
                    
                # Menggunakan jenis barang saat ini sebagai default radio
                current_jenis = selected_row.get('jenis', 'Stok') if selected_row else 'Stok'
                new_jenis = col_jenis.radio(
                    "Tujuan Alokasi SN", 
                    ['Stok', 'Display'], 
                    index=['Stok', 'Display'].index(current_jenis),
                    key="radio_jenis_tab1" # Updated Key
                )
                
                st.markdown("##### 📝 Scan SN di Bawah (Satu SN per Baris)")
                
                col_scan, col_exist = st.columns([2, 1])
                
                # Input Area
                batch_input = col_scan.text_area(
                    "Scan SN List", 
                    placeholder="Scan SN pertama...\nScan SN kedua...\n[Tekan Ctrl+Enter atau Tombol Simpan]",
                    height=250
                )
                
                # FIX V1.22: Display SN yang sudah tercatat
                if selected_row:
                    current_sn_list = selected_row.get('sn_list', [])
                    if current_sn_list:
                        sn_display = "\n".join(current_sn_list)
                        col_exist.text_area(
                            f"SN Sudah Tercatat ({len(current_sn_list)})",
                            value=sn_display,
                            height=250,
                            disabled=True
                        )
                    else:
                        col_exist.info("Belum ada SN tercatat.")


                if st.form_submit_button("💾 SUBMIT & SIMPAN SN BATCH", type="primary", use_container_width=True):
                    
                    if not selected_row:
                        st.error("Pilih Barang SN yang valid terlebih dahulu.")
                        st.stop()
                        
                    submitted_sns = [s.strip() for s in batch_input.split('\n') if s.strip()]
                    
                    if not submitted_sns:
                        st.warning("Tidak ada Serial Number yang dimasukkan.")
                        st.stop()
                    
                    # Pemrosesan Batch
                    current_sn_list = selected_row.get('sn_list', [])
                    final_sn_list = current_sn_list[:]
                    new_count = 0
                    
                    for sn in submitted_sns:
                        if sn not in final_sn_list:
                            final_sn_list.append(sn)
                            new_count += 1
                        else:
                            st.warning(f"SN `{sn}` sudah ada di list sebelumnya, dilewati.")
                    
                    # Keterangan diabaikan untuk Global Scan, hanya fokus pada SN/Jenis
                    updates, conflict = handle_update_sn_list(
                        selected_row, final_sn_list, new_jenis, final_nama_user, loaded_time, 
                        selected_row.get('keterangan')
                    )

                    if not conflict and updates > 0:
                        st.success(f"✅ {new_count} SN baru ditambahkan untuk **{selected_row['nama_barang']}**! Total: {len(final_sn_list)}")
                        time.sleep(1) 
                        st.rerun() 
                    elif conflict:
                         st.error("Gagal simpan SN. Mencoba perbaikan otomatis (cache clear)...")
                         st.cache_resource.clear()
                         st.rerun()
        else:
            st.info("Tidak ada item SN yang aktif dalam sesi ini.")

    # -------------------------------------------------------------------------
    # TAB 2: Input Qty Non-SN
    # -------------------------------------------------------------------------
    with tab_non_sn:
        if not df_non.empty:
            st.subheader(f"📦 Non-SN ({len(df_non)}) - Input Kuantitas")

            for index, row in df_non.iterrows():
                item_id = row['id']
                qty_po = row['qty_po']
                default_qty = row['qty_fisik']
                default_jenis = row['jenis']
                selisih_po = default_qty - qty_po
                
                status_text = "MATCH" if selisih_po == 0 else ("OVER" if selisih_po > 0 else "SHORT")
                status_color = "green" if selisih_po == 0 else "red"
                
                header_text = f"**{row['nama_barang']}** (PO: {qty_po}) | Selisih: :{status_color}[{selisih_po}]"
                
                notes_key = f"notes_non_{item_id}"
                current_notes = row.get('keterangan', '') if row.get('keterangan') is not None else ''
                
                # Card Non-SN (sembunyi default)
                with st.expander(header_text, expanded=False):
                    col_info, col_input = st.columns([1.5, 1.5])
                    
                    with col_info:
                        st.markdown(f"**SKU:** {row['sku']}")
                        st.markdown(f"**Qty PO (Harapan):** `{qty_po}`")
                        st.markdown(f"**Dicek Oleh:** {row['updated_by']}")
                        if current_notes: st.markdown(f"**Catatan Sebelumnya:** `{current_notes}`")
                    
                    with col_input:
                        new_qty = st.number_input("JML FISIK DITERIMA", value=default_qty, min_value=0, step=1, key=f"qty_non_{item_id}")
                        
                        new_jenis = st.radio("Tujuan Alokasi", ['Stok', 'Display'], index=['Stok', 'Display'].index(default_jenis), horizontal=True, key=f"jenis_non_{item_id}")
                        
                        keterangan = st.text_area("Keterangan/Isu (Opsional)", value=current_notes, key=notes_key, height=50)

                        if st.button("Simpan Non-SN", key=f"btn_non_{item_id}", type="primary", use_container_width=True):
                            updates, conflict = handle_update_non_sn(row, new_qty, new_jenis, final_nama_user, loaded_time, keterangan.strip())
                            
                            if not conflict and updates > 0:
                                st.toast(f"✅ Qty {row['nama_barang']} ({new_jenis}) disimpan!", icon="💾")
                                time.sleep(0.5)
                                st.rerun()
                            elif not conflict:
                                st.info("Tidak ada perubahan yang tersimpan.")
                            elif conflict:
                                st.error("Gagal simpan Non-SN. Mencoba perbaikan otomatis (cache clear)...")
                                st.cache_resource.clear()
                                st.rerun()
        else:
            st.info("Tidak ada item Non-SN yang aktif dalam sesi ini.")

    # -------------------------------------------------------------------------
    # TAB 3: Tambah Ad Hoc (Blind Receive) - New Structure
    # -------------------------------------------------------------------------
    with tab_adhoc:
        st.subheader("👻 Registrasi Barang Tanpa Dokumen (Blind Receive)")
        st.warning("Gunakan fitur ini dengan bijak, karena akan mencatat item yang TIDAK ADA di dokumen GR/PO.")
        
        # --- FIX V1.20: Tipe Barang dan Tujuan di luar form untuk reaktivitas ---
        col_tipe, col_jenis = st.columns(2)
        blind_tipe = col_tipe.radio("Tipe Barang", ['NON-SN', 'SN'], index=0, horizontal=True, key="blind_tipe_radio")
        blind_jenis = col_jenis.radio("Tujuan Alokasi", ['Stok', 'Display'], index=0, horizontal=True, key="blind_jenis_radio")

        with st.form("blind_receive_form", clear_on_submit=True):
            
            # Input Brand dan SKU
            col_brand, col_sku = st.columns(2)
            blind_brand = col_brand.text_input("Brand", placeholder="Contoh: Samsung/Vivan/Robot")
            blind_sku = col_sku.text_input("SKU Barang", placeholder="Contoh: S24-ULT-512")
            
            st.markdown("---")
            
            # --- Conditional Input (Digerakkan oleh blind_tipe di luar form) ---
            blind_qty = 0
            blind_sn_list = None
            
            if blind_tipe == 'NON-SN':
                blind_qty = st.number_input("Quantity Fisik Diterima", min_value=1, step=1)
                st.caption("Item akan di-insert sebagai 1 baris data Non-SN.")
            else:
                blind_sn_input = st.text_area(
                    "Scan SN List (Satu SN per Baris)", 
                    height=150, 
                    placeholder="Scan SN pertama...\nScan SN kedua..."
                )
                blind_sn_list = [s.strip() for s in blind_sn_input.split('\n') if s.strip()]
                if blind_sn_list:
                    st.info(f"Total SN yang discan: **{len(blind_sn_list)}** (Ini akan menjadi Qty Fisik)")
            
            st.markdown("---")
            blind_keterangan = st.text_area("Keterangan Tambahan (Wajib)", height=50)

            if st.form_submit_button("➕ REGISTRASI BLIND RECEIVE (INSERT BARU)", type="secondary", use_container_width=True):
                
                # Check umum
                if not blind_brand or not blind_sku or not blind_keterangan.strip():
                    st.error("Brand, SKU, dan Keterangan wajib diisi.")
                    st.stop()
                    
                success, msg = handle_blind_insert(
                    blind_brand, blind_sku, blind_qty, blind_sn_list, blind_tipe, blind_jenis, blind_keterangan, final_nama_user
                )
                
                if success:
                    st.success(f"✅ Registrasi Blind Receive berhasil! Item: {blind_brand} ({blind_sku})")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Gagal Registrasi: {msg}")


    # -------------------------------------------------------------------------
    # TAB 4: Status & Review (Display Only)
    # -------------------------------------------------------------------------
    with tab_status:
        st.subheader(f"📋 Status Barang SN ({len(df_sn)})")

        if not df_sn.empty:
            for index, row in df_sn.iterrows():
                item_id = row['id']
                qty_po = row['qty_po']
                qty_fisik = len(row.get('sn_list', []))
                default_jenis = row['jenis']
                selisih_po = qty_fisik - qty_po
                
                status_text = "MATCH" if selisih_po == 0 else ("OVER" if selisih_po > 0 else "SHORT")
                status_color = "green" if selisih_po == 0 else "red"
                
                # FIX V1.23: Tampilkan status Inbound di header status
                inbound_status = "✅ INBOUND OK" if row.get('is_inbound') else "⏳ BELUM INBOUND"
                inbound_color = "blue" if row.get('is_inbound') else "orange"
                
                header_text = f"**{row['nama_barang']}** | Tercatat: {qty_fisik} | Selisih: :{status_color}[{selisih_po}] | Alokasi: {default_jenis} | Status: :{inbound_color}[{inbound_status}]"
                
                # Card Status SN (sembunyi default)
                with st.expander(header_text, expanded=False):
                    st.markdown(f"**SKU:** {row['sku']}")
                    st.markdown(f"**Dicek Oleh:** {row['updated_by']}")
                    if row.get('keterangan'): st.markdown(f"**Catatan:** `{row['keterangan']}`")
                    
                    # Tombol untuk melihat SN list
                    if qty_fisik > 0:
                        st.markdown("##### SN List yang sudah tercatat:")
                        sn_display = "\n".join(row.get('sn_list', []))
                        st.code(sn_display, language='text')
        else:
             st.info("Tidak ada item SN dalam sesi ini.")

        st.markdown("---")
        
        if not df_non.empty:
            st.subheader(f"📦 Status Barang Non-SN ({len(df_non)})")
            
            # Menampilkan Non-SN dalam bentuk tabel sederhana untuk review
            df_review = df_non[['sku', 'nama_barang', 'qty_po', 'qty_fisik', 'jenis', 'updated_by', 'keterangan', 'is_inbound']].copy()
            df_review['Selisih'] = df_review['qty_fisik'] - df_review['qty_po']
            
            # FIX V1.23: Format Inbound Status
            df_review['Status Inbound'] = df_review['is_inbound'].apply(lambda x: "OK" if x else "PENDING")
            df_review = df_review.drop(columns=['is_inbound'])
            
            st.dataframe(df_review, use_container_width=True)
        else:
            st.info("Tidak ada item Non-SN dalam sesi ini.")
            
# --- FUNGSI ADMIN ---
def page_admin():
    st.title("🛡️ Admin Dashboard (Receiving)")
    active_grs = get_active_session_info()
    
    # Menghilangkan BLIND-RECEIVE dari daftar yang harus diadministrasi
    admin_active_grs = [gr for gr in active_grs if gr != "BLIND-RECEIVE" and gr != "- Error Koneksi -"]
    
    if not admin_active_grs:
        st.warning("⚠️ Belum ada sesi GR aktif yang di-upload.")
    else:
        st.info(f"📅 Sesi Aktif: **{', '.join(admin_active_grs)}**")
    
    tab1, tab2, tab3, tab_inbound, tab_maintenance = st.tabs([
        "🚀 Mulai Sesi GR", 
        "🗄️ Laporan & Arsip", 
        "⚠️ Danger Zone", 
        "📦 Inbound Control",
        "🔧 Maintenance"
    ])
    
    with tab1:
        st.markdown("### 1️⃣ Download Template Master GR/PO")
        st.caption("Gunakan template ini untuk menyusun data GR/PO yang akan di-upload.")
        st.download_button("⬇️ Download Template Master GR/PO", get_master_template_excel_receiving(), "Template_Master_Receiving.xlsx")
        
        st.write("---")

        st.markdown("### 2️⃣ Mulai Sesi Penerimaan Baru")
        st.caption("Upload File Master GR/PO di sini. Sesi yang di-upload akan menjadi AKTIF.")
        
        gr_number = st.text_input("Nomor GR/PO Baru", placeholder="Contoh: GR/2025/11/001")
        file_master = st.file_uploader("Upload File Master GR/PO", type="xlsx", key="u_main_gr")
        
        if file_master and gr_number:
            if st.button("🔥 MULAI SESI RECEIVING BARU", type="primary"):
                with st.spinner("Meng-upload Data GR..."):
                    df = pd.read_excel(file_master)
                    # FIX V1.19: Tidak lagi menonaktifkan sesi lama
                    ok, msg = process_and_insert(df, gr_number.strip())
                    if ok: st.success(f"Sesi '{gr_number.strip()}' Dimulai! {msg} data GR masuk."); time.sleep(2); st.cache_data.clear(); st.rerun()
                    else: st.error(f"Gagal: {msg}")


    with tab2:
        st.markdown("### 📊 Laporan Penerimaan")
        
        # Mengambil semua GR, termasuk yang Blind Receive
        all_gr_numbers = get_active_session_info()
        all_archived_grs = sorted(list(set([x['gr_number'] for x in supabase.table(RECEIVING_TABLE).select("gr_number").eq("is_active", False).execute().data])) if supabase.table(RECEIVING_TABLE).select("gr_number").execute().data else [])
        
        gr_report_options = (
            ["-- Pilih Dokumen --"] + 
            [f"AKTIF: {gr}" for gr in admin_active_grs] +
            [f"AKTIF: BLIND-RECEIVE"] +
            [f"ARSIP: {gr}" for gr in all_archived_grs]
        )
        
        selected_report_str = st.selectbox("Pilih Dokumen untuk Laporan:", gr_report_options)
        
        df = pd.DataFrame()
        report_name = ""
        is_active_session = False
        
        if selected_report_str.startswith("AKTIF:"):
            report_name = selected_report_str.split("AKTIF: ")[1]
            df = get_data(gr_number=report_name, only_active=True)
            is_active_session = True
        elif selected_report_str.startswith("ARSIP:"):
            report_name = selected_report_str.split("ARSIP: ")[1]
            df = get_data(gr_number=report_name, only_active=False)

        if not df.empty and report_name:
            st.markdown("---")
            df['qty_diff'] = df['qty_fisik'] - df['qty_po']
            
            total_sku = len(df)
            total_po = df['qty_po'].sum()
            total_fisik = df['qty_fisik'].sum()
            total_diff = df['qty_diff'].sum()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total SKU", total_sku)
            c2.metric("Total Qty PO", total_po)
            c3.metric("Total Qty Fisik", total_fisik)
            c4.metric("Total Selisih", total_diff)
            
            st.dataframe(df[['gr_number', 'sku', 'nama_barang', 'kategori_barang', 'jenis', 'qty_po', 'qty_fisik', 'qty_diff', 'keterangan', 'is_inbound', 'updated_by', 'updated_at']])
            
            # --- FIX V1.22: Hapus Item Blind Receive ---
            if report_name == "BLIND-RECEIVE" and is_active_session:
                st.markdown("### 🗑️ Hapus Item Blind Receive (Review)")
                blind_items = df[['id', 'nama_barang', 'sku', 'qty_fisik', 'keterangan']].copy()
                blind_items['Display'] = blind_items['nama_barang'] + " (" + blind_items['sku'] + f") - Qty: {blind_items['qty_fisik']}"
                
                item_to_delete_id = st.selectbox(
                    "Pilih Item Blind Receive untuk Dihapus:", 
                    options=["-- Pilih Item --"] + list(blind_items['Display']),
                    key="blind_delete_selector"
                )
                
                if item_to_delete_id != "-- Pilih Item --":
                    item_id = blind_items[blind_items['Display'] == item_to_delete_id]['id'].iloc[0]
                    if st.button(f"🔥 KONFIRMASI HAPUS: {item_to_delete_id}", type="primary"):
                        success, msg = delete_blind_receive_item(item_id)
                        if success:
                            st.success(f"✅ Item '{item_to_delete_id}' berhasil dihapus.")
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(f"Gagal menghapus: {msg}")
            
            st.markdown("### 📥 Download Laporan")
            tgl = datetime.now().strftime('%Y-%m-%d')
            st.download_button(f"📥 Download Laporan {report_name}", convert_df_to_excel(df), f"Laporan_GR_{report_name}_{tgl}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            
            # Tambahkan fungsi arsip sesi yang aktif
            if is_active_session and report_name != "BLIND-RECEIVE":
                if st.button(f"✅ ARSIPKAN SESI {report_name}", type="secondary"):
                     try:
                        supabase.table(RECEIVING_TABLE).update({"is_active": False}).eq("gr_number", report_name).execute()
                        st.success(f"Sesi {report_name} berhasil diarsipkan!")
                        st.cache_data.clear()
                        time.sleep(2); st.rerun()
                     except Exception as e:
                         st.error(f"Gagal mengarsipkan: {e}")


    with tab3:
        st.header("⚠️ DANGER ZONE")
        st.error("Tindakan di sini bersifat permanen.")
        
        st.markdown(f"**Menghapus SEMUA Sesi Aktif:** Ini akan menghapus **SELURUH** data sesi GR yang sedang berjalan (is_active=True) tanpa arsip. Hati-hati.")
        
        st.divider()
        input_pin = st.text_input("Masukkan PIN Keamanan", type="password", placeholder="PIN Standar: 123456", key="final_pin")
        st.session_state['confirm_reset_state'] = st.checkbox("Saya sadar data sesi ini akan hilang permanen.", key="final_check")
        
        if st.button("🔥 HAPUS SEMUA SESI AKTIF", use_container_width=True):
            if input_pin == RESET_PIN:
                if st.session_state.get('confirm_reset_state', False): 
                    with st.spinner("Menghapus Sesi Aktif..."):
                        ok, msg = delete_active_session()
                        if ok: st.success("Semua Sesi Aktif berhasil di-reset!"); st.cache_data.clear(); time.sleep(2); st.rerun()
                        else: st.error(f"Gagal: {msg}")
                else:
                    st.error("Harap centang konfirmasi dulu.")
            else:
                st.error("PIN Salah.")
                
    with tab_inbound:
        st.header("📦 Kontrol Status Inbound")
        st.caption("Supervisor menandai item yang SUDAH divalidasi dan SUDAH dipindahkan ke area akhir (Display/Stok).")
        
        # Ambil semua data AKTIF yang sudah divalidasi tetapi BELUM Inbound
        df_inbound_pending = get_data(only_active=True)
        # Filter: qty_fisik > 0 DAN is_inbound == False
        df_inbound_pending = df_inbound_pending[
            (df_inbound_pending['qty_fisik'] > 0) & 
            (df_inbound_pending['is_inbound'] == False)
        ].copy()
        
        if df_inbound_pending.empty:
            st.success("🎉 Tidak ada item yang menunggu status INBOUND.")
        else:
            st.info(f"Ditemukan {len(df_inbound_pending)} item menunggu konfirmasi Inbound.")
            
            # Persiapan Data untuk Diproses
            inbound_options = ["-- Pilih Item untuk Inbound --"] + [
                f"{row['gr_number']} | {row['nama_barang']} ({row['qty_fisik']} unit) | SKU: {row['sku']}"
                for _, row in df_inbound_pending.iterrows()
            ]
            
            selected_inbound_item = st.selectbox(
                "Pilih Item Selesai Inbound:", 
                options=inbound_options
            )
            
            if selected_inbound_item != "-- Pilih Item --":
                # Mendapatkan ID dari baris yang dipilih
                item_details = df_inbound_pending[
                    (df_inbound_pending['gr_number'] == selected_inbound_item.split(' | ')[0].strip()) &
                    (df_inbound_pending['sku'] == selected_inbound_item.split('SKU: ')[1].strip())
                ].iloc[0]
                
                if st.button(f"✅ KONFIRMASI INBOUND: {item_details['nama_barang']}", type="primary"):
                    # Nama Admin (dari sidebar)
                    admin_name = st.session_state[SESSION_KEY_CHECKER]
                    if admin_name == "-- Pilih Petugas --":
                         st.error("Pilih nama Anda di sidebar sebelum konfirmasi Inbound.")
                    else:
                        success, msg = update_inbound_status(item_details['id'], item_details['gr_number'], admin_name)
                        if success:
                            st.success(f"Status INBOUND berhasil diperbarui untuk {item_details['nama_barang']}!")
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(f"Gagal: {msg}")
                            
            st.markdown("---")
            st.dataframe(df_inbound_pending[['gr_number', 'sku', 'nama_barang', 'qty_fisik', 'jenis', 'updated_by', 'updated_at']], use_container_width=True)

    with tab_maintenance:
        st.header("🔧 Debugging & Cache Maintenance")
        st.caption("Gunakan ini hanya jika Anda mendapat error aneh setelah mengganti Kunci API atau RLS.")
        if st.button("🗑️ HAPUS SEMUA CACHE STREAMLIT", type="secondary"):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.success("Cache Data dan Koneksi berhasil dihapus! Aplikasi akan di-refresh.")
            st.rerun()


# --- MAIN ---
def main():
    st.set_page_config(page_title="GR Validation v1.23", page_icon="📦", layout="wide")
    # FIX V1.19: Sidebar hanya menampilkan Nama Aplikasi dan Navigasi
    st.sidebar.title("GR Validation Apps v1.23")
    menu = st.sidebar.radio("Navigasi", ["Checker Input", "Admin Panel"])
    if menu == "Checker Input": page_checker()
    elif menu == "Admin Panel":
        pwd = st.sidebar.text_input("Password Admin", type="password")
        if pwd == "admin123": page_admin()

if __name__ == "__main__":
    if SESSION_KEY_CHECKER not in st.session_state:
        st.session_state[SESSION_KEY_CHECKER] = "-- Pilih Petugas --"
    main()
